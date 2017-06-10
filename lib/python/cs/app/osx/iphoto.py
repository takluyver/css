#!/usr/bin/python
#
# Access iPhoto data.
#       - Cameron Simpson <cs@zip.com.au> 04apr2013
#

from __future__ import print_function
import sys
import os
import os.path
from collections import namedtuple
from fnmatch import fnmatch
from functools import partial
import pprint
from getopt import GetoptError
import re
import sqlite3
from threading import RLock
from PIL import Image
Image.warnings.simplefilter('error', Image.DecompressionBombWarning)
from .plist import PListDict, ingest_plist
from cs.dbutils import TableSpace, Table, Row
from cs.edit import edit_strings
from cs.env import envsub
from cs.lex import get_identifier
from cs.logutils import Pfx, debug, info, warning, error, setup_logging, X, XP
from cs.obj import O
from cs.py.func import prop
from cs.seq import the
from cs.threads import locked, locked_property
from cs.xml import pprint as xml_pprint

DEFAULT_LIBRARY = '$HOME/Pictures/iPhoto Library.photolibrary'

USAGE = '''Usage: %s [/path/to/iphoto-library-path] op [op-args...]
  info masters      List info about masters.
  ls                List apdb names.
  ls [0-5]          List master pathnames with specific rating.
  ls albums         List album names.
  ls events         List events names.
  ls folders        List folder names (includes events).
  ls keywords       List keywords.
  ls masters        List master pathnames.
  ls people         List person names.
  rename {events|keywords|people} {/regexp|name}...
                    Rename entities.
  select criteria... List masters with all specified criteria.
  test [args...]    Whatever I'm testing at the moment...

Criteria:
  [!]/regexp            Filename matches regexp.
  [!]kw:[keyword]       Latest version has keyword.
                        Empty keyword means "has a keyword".
  [!]face:[person_name] Latest version has named person.
                        Empty person_name means "has a face".
                        May also be writtens "who:...".
  [!]attr{<,<=,=,>=,>}value
                        Test image attribute eg "width>=1920".
  Because "!" is often used for shell history expansion, a dash "-"
  is also accepted to invert the selector.
'''

def main(argv=None):
  ''' Main program associated with the cs.app.osx.iphoto module.
  '''
  if argv is None:
    argv = [ 'cs.app.osx.iphoto' ]
  cmd = os.path.basename(argv.pop(0))
  usage = USAGE % (cmd,)
  setup_logging(cmd)
  with Pfx(cmd):
    badopts = False
    if argv and argv[0].startswith('/'):
      library_path = argv.pop(0)
    else:
      library_path = os.environ.get('IPHOTO_LIBRARY_PATH', envsub(DEFAULT_LIBRARY))
    I = iPhoto(library_path)
    xit = 0
    if not argv:
      warning("missing op")
      badopts = True
    else:
      op = argv.pop(0)
      with Pfx(op):
        try:
          if op == 'info':
            xit = cmd_info(I, argv)
          elif op == 'ls':
            xit = cmd_ls(I, argv)
          elif op == 'rename':
            xit = cmd_rename(I, argv)
          elif op == 'select':
            xit = cmd_select(I, argv)
          elif op == "test":
            xit = cmd_test(I, argv)
          else:
            raise GetoptError("unrecognised op")
        except GetoptError as e:
          warning("usage: %s", e)
          badopts = True
    if badopts:
      print(usage, file=sys.stderr)
      return 2
    return xit

def cmd_info(I, argv):
  ''' Usage: info masters...
  '''
  xit = 0
  if not argv:
    raise GetoptError("missing masters")
  obclass = argv.pop(0)
  with Pfx(obclass):
    if obclass == 'masters':
      for master in sorted(I.masters(), key=lambda m: m.pathname):
        with Pfx(master.pathname):
          iminfo = master.image_info
          if iminfo is None:
            error("no info")
            xit = 1
          else:
            print(master.pathname, iminfo.dx, iminfo.dy, iminfo.format,
                  *[ 'kw:'+kwname for kwname in master.keyword_names ])
    else:
      raise GetoptError("unknown class: %r" % (obclass,))
  return xit

def cmd_ls(I, argv):
  xit = 0
  get_row_map = {
    'albums':   I.albums,
    'events':   I.folders,
    'folders':  I.folders,
    'keywords': I.keywords,
    'masters':  I.masters,
    'people':   I.persons,
  }
  if not argv:
    for obclass_name in sorted(get_row_map.keys()):
      print(obclass_name)
  else:
    obclass = argv.pop(0)
    with Pfx(obclass):
      if obclass.isdigit():
        rating = int(obclass)
        I.load_versions()
        names = []
        for version in I.versions():
          if version.mainRating == rating:
            pathname = version.master.pathname
            if pathname is not None:
              names.append(pathname)
      try:
        get_rows = get_row_map[obclass]
      except KeyError:
        raise GetoptError("unknown class")
      rows = get_rows()
      if obclass == 'events':
        if not argv:
          argv = I.event_names()
      elif obclass == 'folders':
        if not argv:
          argv = I.folder_names()
      def row_key(row):
        key = getattr(row, 'name', None)
        if key is None:
          key = str(row.modelId)
        return key
      for row in sorted(rows, key=row_key):
        key = row_key(row)
        with Pfx(key):
          if argv and key not in argv:
            continue
          print(key)
          if argv:
            for column_name in sorted(row.column_names):
              if column_name.endswith('Data'):
                obj = ingest_plist(row[column_name], recurse=True, resolve=True)
                print(' ', column_name+':')
                pprint.pprint(obj.value, width=32)
              else:
                print(' ', column_name+':', row[column_name])
          if obclass == 'albums':
            print("apalbumpath =", row.apalbum_path)
            apalbum = row.apalbum
            if apalbum is None:
              error("NO ALBUM DATA?")
            else:
              print("filter:")
              apalbum.dump()
  return xit

def cmd_rename(I, argv):
  ''' Usage: rename {events|keywords|people} {/regexp|name}...
  '''
  xit = 0
  if not argv:
    raise GetoptError("missing events")
  obclass = argv.pop(0)
  with Pfx(obclass):
    if obclass == 'events':
      table = I.folder_table
      I.load_folders()
      get_items = I.events
    elif obclass == 'keywords':
      table = I.keyword_table
      I.load_keywords()
      get_items = I.read_keywords
    elif obclass == 'people':
      table = I.person_table
      I.load_persons()
      get_items = I.persons
    else:
      raise GetoptError("known class")
    all_names = set(item.name for item in get_items())
    if argv:
      edit_lines = set()
      for arg in argv:
        with Pfx(arg):
          # TODO: select by regexp if /blah
          if arg.startswith('/'):
            regexp = re.compile(arg[1:-1] if arg.endswith('/') else arg[1:])
            edit_lines.update(item.edit_string
                              for item in get_items()
                              if regexp.search(item.name))
          elif '*' in arg or '?' in arg:
            edit_lines.update(item.edit_string
                              for item in get_items()
                              if fnmatch(item.name, arg))
          elif arg in all_names:
            for item in get_items():
              if item.name == arg:
                edit_lines.add(item.edit_string)
          else:
            raise GetoptError("unknown item name")
    else:
      edit_lines = set(item.edit_string for item in get_items())
    changes = edit_strings(sorted(edit_lines,
                                  key=lambda _: _.split(':', 1)[1]),
                           errors=lambda msg: warning(msg + ', discarded')
                          )
    for old_string, new_string in changes:
      with Pfx("%s => %s", old_string, new_string):
        old_modelId, old_name = old_string.split(':', 1)
        old_modelId = int(old_modelId)
        try:
          new_modelId, new_name = new_string.split(':', 1)
          new_modelId = int(new_modelId)
        except ValueError as e:
          error("invalid edited string: %s", e)
          xit = 1
        else:
          if old_modelId != new_modelId:
            error("modelId changed")
            xit = 1
          elif new_name in all_names:
            if obclass == 'keywords':
              # TODO: merge keywords
              print("%d: merge %s => %s" % (old_modelId, old_name, new_name))
              otherModelId = the(item.modelId
                                 for item in get_items()
                                 if item.name == new_name)
              I.replace_keywords(old_modelId, otherModelId)
              I.expunge_keyword(old_modelId)
            else:
              error("new name already in use: %r", new_name)
              xit = 1
          else:
            print("%d: %s => %s" % (old_modelId, old_name, new_name))
            table[old_modelId].name = new_name
  return xit

def cmd_select(I, argv):
  xit = 0
  badopts = False
  if not argv:
    raise GetoptError("missing selectors")
  selectors = []
  for selection in argv:
    with Pfx(selection):
      try:
        selector = I.parse_selector(selection)
      except ValueError as e:
        warning("invalid selector: %s", e)
        badopts = True
      else:
        selectors.append(selector)
  if badopts:
    raise GetoptError("invalid arguments")
  masters = None
  for selector in selectors:
    masters = selector.select(masters)
    for master in masters:
      print(master.pathname)
  return xit

def cmd_test(I, argv):
  AD = I.load_albumdata()
  print('AlbumData.xml:')
  print(pprint.pformat(AD._as_dict(), indent=2, width=32))
  sys.exit(1)
  ##for folder in I.read_folders():
  ##  print('uuid =', folder.uuid, 'folderType =', folder.folderType, 'name =', folder.name)
  ##for keyword in I.read_keywords():
  ##  print('uuid =', keyword.uuid, 'name =', keyword.name)
  ##for master in I.read_masters():
  ##  print('uuid =', master.uuid, 'name =', master.name, 'originalFileName =', master.originalFileName, 'imagePath =', master.imagePath, 'pathname =', master.pathname)
  ##for v in I.read_versions():
  ##  print('uuid =', v.uuid, 'masterUuid =', v.masterUuid, 'versionNumber =', v.versionNumber)
  ##for kwv in I.keywords_to_versions():
  ##  print('keywordId =', kwv.keywordId, 'versionId =', kwv.versionId)
  ##for album in I.album_table.read_rows():
  ##  print('uuid =', album.uuid, 'albumType =', album.albumType, 'name =', album.name)
  ##for kwname in sorted(I.keyword_names()):
  ##  kw = I.keyword_by_name.get(kwname)
  ##  print(kw.name, [ master.name for master in kw.masters() ])
  for vface in I.vfaces():
    print(vface)
    P = vface.person()
    print("Person", P)
    master = vface.master
    print("Master", master)
    print(" ", master.pathname)
    for vface2 in master.vfaces:
      print("  VFace", vface)
    FI = vface.Image()
    filebase, ext = os.path.splitext(os.path.basename(master.pathname))
    filename = '%s-FACE%s' % (filebase, ext)
    print("save to %r" % (filename,))
    FI.save(filename)
    FI.close()
    break
  return 0

Image_Info = namedtuple('Image_Info', 'dx dy format')

class iPhoto(O):
  ''' Access an iPhoto library.
      This contains multiple sqlite3 databases.
  '''

  def __init__(self, libpath=None):
    ''' Open the iPhoto library stored at `libpath`.
        If `libpath` is not supplied, use DEFAULT_LIBRARY.
    '''
    if libpath is None:
      libpath = envsub(DEFAULT_LIBRARY)
    if not os.path.isdir(libpath):
      raise ValueError("not a directory: %r" % (libpath,))
    self.path = libpath
    self.table_by_nickname = {}
    self._lock = RLock()
    self.dbs = iPhotoDBs(self)
    self.dbs.load_all()

  def pathto(self, rpath):
    if rpath.startswith('/'):
      raise ValueError('rpath may not start with a slash: %r' % (rpath,))
    return os.path.join(self.path, rpath)

  def db_names(self):
    return self.dbs.db_names()

  def dbpath(self, db_name):
    return self.dbs.pathto(db_name)

  def __getattr__(self, attr):
    if not attr.startswith('_'):
      if attr.endswith('s'):
        if '_' not in attr:
          # *s ==> iterable of * (obtained from *_by_id)
          nickname = attr[:-1]
          if nickname in self.table_by_nickname:
            # require the matching table load
            getattr(self, 'load_%ss' % (nickname,))()
            by_id = getattr(self, nickname + '_by_id')
            return lambda: by_id.values()
        # read_*s ==> iterator of rows from table "*"
        if attr.startswith('read_'):
          nickname = attr[5:-1]
          return self.table_by_nickname[nickname].read_rows
      if attr.startswith('load_') and attr.endswith('s'):
        nickname = attr[5:-1]
        if nickname in self.table_by_nickname:
          loaded_attr = '_loaded_table_' + nickname
          loaded = getattr(self, loaded_attr, False)
          if loaded:
            return lambda: None
          else:
            load_funcname = '_load_table_' + nickname + 's'
            ##@locked
            def loadfunc():
              if not getattr(self, loaded_attr, False):
                XP("load %ss (%s)...", nickname, self.table_by_nickname[nickname].qual_name)
                getattr(self, load_funcname)()
                setattr(self, loaded_attr, True)
            return loadfunc
      if attr.startswith('select_by_'):
        criterion_words = attr[10:].split('_')
        class_name = 'SelectBy' + '_'.join(word.title() for word in criterion_words)
        return partial(globals()[class_name], self)
      if attr.endswith('_table'):
        # *_table ==> table "*"
        nickname = attr[:-6]
        if nickname in self.table_by_nickname:
          return self.table_by_nickname[nickname]
    raise AttributeError("iPhoto.__getattr__: nothing named %r" % (attr,))

  def _load_table_albums(self):
    ''' Load Library.RKMaster into memory and set up mappings.
    '''
    with Pfx("_load_table_albums"):
      by_id = self.album_by_id = {}
      ##by_uuid = self.album_by_uuid = {}
      by_name = self.albums_by_name = {}
      for album in self.read_albums():
        by_id[album.modelId] = album
        ##by_uuid[album.uuid] = album
        name = album.name
        if name is None:
          debug("album has no name: %s", album.uuid)
          pass
        else:
          try:
            albums = by_name[name]
          except KeyError:
            albums = by_name[name] = set()
          albums.add(album)

  def album(self, album_id):
    self.load_albums()
    return self.album_by_id.get(album_id)

  def album_names(self):
    self.load_albums()
    return self.albums_by_name.keys()

  @property
  def albumdata_path(self):
    ''' Pathname of the AlbumData.xml file, saved when iPhoto quits.
    '''
    return self.pathto('AlbumData.xml')

  def load_albumdata(self):
    return ingest_plist(self.albumdata_path, recurse=True, resolve=True)

  @locked_property
  def albumdata_xml_plist(self):
    ''' Ingest and cache the AlbumData.xml file.
    '''
    return self.load_albumdata()

  def _load_table_faces(self):
    ''' Load Faces.RKDetectedFace into memory and set up mappings.
    '''
    by_id = self.face_by_id = {}
    by_master_uuid = self.faces_by_master_uuid = {}
    for face in self.read_faces():
      by_id[face.modelId] = face
      muuid = face.masterUuid
      try:
        faces = by_master_uuid[muuid]
      except KeyError as e:
        faces = by_master_uuid[muuid] = set()
      faces.add(face)

  def face(self, face_id):
    return self.face_by_id.get(face_id)

  def _load_table_vfaces(self):
    ''' Load Faces.RKVersionFaceContent into memory and set up mappings.
    '''
    by_id = self.vface_by_id = {}
    by_master_id = self.vfaces_by_master_id
    for vface in self.read_vfaces():
      by_id[vface.modelId] = vface
      master_id = vface.masterId
      try:
        vfaces = by_master_id[master_id]
      except KeyError:
        vfaces = by_master_id[master_id] = set()
      vfaces.add(vface)

  @locked_property
  def vfaces_by_master_id(self):
    self.load_vfaces()
    return I.vfaces_by_master_id.get(self.modelId, ())

  def _load_table_folders(self):
    ''' Load Library.RKFolder into memory and set up mappings.
    '''
    by_id = self.folder_by_id = {}
    by_name = self.folders_by_name = {}
    for folder in self.read_folders():
      by_id[folder.modelId] = folder
      name = folder.name
      try:
        folders = by_name[name]
      except KeyError:
        folders = by_name[name] = set()
      folders.add(folder)

  def folder(self, folder_id):
    self.load_folders()
    return self.folder_by_id.get(folder_id)

  def folder_names(self):
    self.load_folders()
    return self.folders_by_name.keys()

  def folders_simple(self):
    return [ folder for folder in self.folders()
             if folder.sortKeyPath == 'custom.default'
           ]

  def event(self, event_id):
    self.load_folders()
    l
  def events(self):
    return [ folder for folder in self.folders()
             if folder.sortKeyPath == 'custom.kind'
           ]

  def event_names(self):
    return [ event.name for event in self.events() ]

  def events_by_name(self, name):
    return [ event for event in self.events() if event.name == name ]

  def _load_table_persons(self):
    ''' Load Faces.RKFaceName into memory and set up mappings.
    '''
    by_id = self.person_by_id = {}
    by_name = self.person_by_name = {}
    by_faceKey = self.person_by_faceKey = {}
    for person in self.read_persons():
      by_id[person.modelId] = person
      by_name[person.name] = person
      by_faceKey[person.faceKey] = person
      # skip fullName; seems to be to associated with Contacts or something

  def person(self, faceKey):
    self.load_persons()
    return self.person_by_faceKey.get(faceKey)

  def person_names(self):
    self.load_persons()
    return self.person_by_name.keys()

  def match_people(self, person_name):
    ''' User convenience: match string against all person names, return matches.
    '''
    lc_person_name = person_name.lower()
    all_names = list(self.person_names())
    matches = set()
    # try exact match, ignoring case
    for name in all_names:
      if lc_person_name == name.lower():
        matches.add(name)
    if not matches:
      # try by word
      lc_person_words = lc_person_name.split()
      for name in all_names:
        lc_words = name.lower().split()
        match_count = 0
        for lc_person_word in lc_person_words:
          if lc_person_word in lc_words:
            match_count += 1
        if match_count == len(lc_person_words):
          matches.add(name)
    if not matches:
      # try substrings
      for name in all_names:
        if lc_person_name in name.lower():
          matches.add(name)
    return matches

  def match_one_person(self, person_name):
    matches = self.match_people(person_name)
    if not matches:
      raise ValueError("unknown person")
    if len(matches) > 1:
      raise ValueError("matches multiple people, rejected: %r" % (matches,))
    return matches.pop()

  def _load_table_masters(self):
    ''' Load Library.RKMaster into memory and set up mappings.
    '''
    by_id = self.master_by_id = {}
    for master in self.read_masters():
      by_id[master.modelId] = master

  def master(self, master_id):
    self.load_masters()
    return self.master_by_id.get(master_id)

  def master_pathnames(self):
    self.load_masters()
    for master in self.master_by_id.values():
      yield master.pathname

  def _load_table_versions(self):
    ''' Load Library.RKVersion into memory and set up mappings.
    '''
    by_id = self.version_by_id = {}
    by_master_id = self.versions_by_master_id = {}
    for version in self.read_versions():
      by_id[version.modelId] = version
      master_id = version.masterId
      try:
        versions = by_master_id[master_id]
      except KeyError:
        versions = by_master_id[master_id] = set()
      versions.add(version)

  def version(self, version_id):
    self.load_versions()
    return self.version_by_id.get(version_id)

  def _load_table_keywords(self):
    ''' Load Library.RKKeyword into memory and set up mappings.
    '''
    by_id = self.keyword_by_id = {}
    by_name = self.keyword_by_name = {}
    for kw in self.read_keywords():
      by_id[kw.modelId] = kw
      by_name[kw.name] = kw

  def keyword(self, keyword_id):
    self.load_keywords()
    return self.keyword_by_id.get(keyword_id)

  def keyword_names(self):
    return frozenset(kw.name for kw in self.keywords())

  def match_keyword(self, kwname):
    ''' User convenience: match string against all keywords, return matches.
    '''
    self.load_keywords()
    if kwname in self.keyword_by_name:
      return (kwname,)
    lc_kwname = kwname.lower()
    matches = []
    for name in self.keyword_names():
      if lc_kwname in name.lower():
        matches.append(name)
    return matches

  def match_one_keyword(self, kwname):
    matches = self.match_keyword(kwname)
    if not matches:
      raise ValueError("unknown keyword")
    if len(matches) > 1:
      raise ValueError("matches multiple keywords, rejected: %r" % (matches,))
    return matches[0]

  def versions_by_keyword(self, kwname):
    self.load_keywords()
    return self.keywords_by_name[kwname].versions()

  def masters_by_keyword(self, kwname):
    self.load_keywords()
    return self.keyword_by_name[kwname].masters()

  def _load_table_keywordForVersions(self):
    ''' Load Library.RKKeywordForVersion into memory and set up mappings.
    '''
    by_kwid = self.kw4v_version_ids_by_keyword_id = {}
    by_vid = self.kw4v_keyword_ids_by_version_id = {}
    for kw4v in self.read_keywordForVersions():
      kwid = kw4v.keywordId
      vid = kw4v.versionId
      try:
        version_ids = by_kwid[kwid]
      except KeyError:
        version_ids = by_kwid[kwid] = set()
      version_ids.add(vid)
      try:
        keyword_ids = by_vid[vid]
      except KeyError:
        keyword_ids = by_vid[vid] = set()
      keyword_ids.add(kwid)

  def keywords_by_version(self, version_id):
    ''' Return version
    '''
    self.load_keywordForVersions()
    kwids = self.kw4v_keyword_ids_by_version_id.get(version_id, ())
    return [ self.keyword(kwid) for kwid in kwids ]

  def replace_keywords(self, old_keyword_id, new_keyword_id):
    ''' Update image tags to replace one keyword with another.
    '''
    self \
      .table_by_nickname['keywordForVersion'] \
      . update_by_column('keywordId', new_keyword_id,
                         'keywordId', old_keyword_id)

  def expunge_keyword(self, keyword_id):
    ''' Remove the specified keyword.
    '''
    self \
      .table_by_nickname['keywordForVersion'] \
      .delete_by_column('keywordId', keyword_id)
    self \
      .table_by_nickname['keyword'] \
      .delete_by_column('modelId', keyword_id)

  def parse_selector(self, selection):
    ''' Parse a single image selection criterion.
        Leading "!" or "-" inverts the test.
        /regexp     Compare image filename against regexp.
        kw:         Image has some keywords.
        kw:kwname   Image has keyword "kwname".
        who:, face: Image contains a face.
        who:name, face:name
                    Image contains the named face.
        attr{<,<=,=,>=,>}value
                    Test image attribute eg "width>=1920".
    '''
    with Pfx(selection):
      selection0 = selection
      selector = None
      invert = False
      if selection.startswith('!') or selection.startswith('-'):
        invert = True
        selection = selection[1:]
      if selection.startswith('/'):
        re_text = selection[1:]
        selector = SelectByFilenameRE(self, re_text, invert)
      else:
        sel_type, offset = get_identifier(selection)
        if not sel_type:
          raise ValueError("expected identifier at %r" % (selection,))
        if offset == len(selection):
          raise ValueError("expected delimiter after %r" % (sel_type,))
        selection = selection[offset:]
        if selection.startswith(':'):
          selection = selection[1:]
          if sel_type == 'kw':
            kwname = selection
            if not kwname:
              selector = SelectByFunction(self,
                                          lambda master: len(master.keywords) > 0,
                                          invert)
            else:
              okwname = kwname
              try:
                kwname = self.match_one_keyword(kwname)
              except ValueError as e:
                raise ValueError("invalid keyword: %s" % (e,))
              else:
                if kwname != okwname:
                  info("%r ==> %r", okwname, kwname)
                selector = SelectByKeyword_Name(self, kwname, invert)
          elif sel_type == 'face' or sel_type == 'who':
            person_name = selection
            if not person_name:
              selector = SelectByFunction(self,
                                          lambda master: len(master.vfaces) > 0,
                                          invert)
            else:
              operson_name = person_name
              try:
                person_name = self.match_one_person(person_name)
              except ValueError as e:
                warning("rejected face name: %s", e)
                badopts = True
              else:
                if person_name != operson_name:
                  info("%r ==> %r", operson_name, person_name)
                selector = SelectByPerson_Name(self, person_name, invert)
          else:
            raise ValueError("unknown selector type %r" % (sel_type,))
        elif selection[0] in '<=>':
          cmpop = selection[0]
          selection = selection[1:]
          if selection.startswith('='):
            cmpop += '='
            selection = selection[1:]
          left = sel_type
          right = selection
          selector = SelectByComparison(self, left, cmpop, right, invert)
        else:
          raise ValueError("unrecognised delimiter after %r" % (sel_type,))
      if selector is None:
        raise RuntimeError("parse_selector(%r) did not set selector" % (selection0,))
      return selector

class iPhotoDBs(object):

  def __init__(self, iphoto):
    self.iphoto = iphoto
    self.dbmap = {}
    self.named_tables = {}
    self._lock = iphoto._lock

  def load_all(self):
    for db_name in 'Library', 'Faces':
      self._load_db(db_name)

  @prop
  def dbdirpath(self):
   return self.iphoto.pathto('Database/apdb')

  def db_names(self):
    for basename in os.listdir(self.dbdirpath):
      if basename.endswith('.apdb'):
        yield basename[:-5]

  def pathto(self, db_name):
    ''' Compute pathname of named database file.
    '''
    if db_name == 'Faces':
      return os.path.join(self.dbdirpath, db_name+'.db')
    return os.path.join(self.dbdirpath, db_name+'.apdb')

  def _opendb(self, dbpath):
    ''' Open an SQLite3 connection to the named database.
    '''
    conn = sqlite3.connect(dbpath)
    XP("connect(%r): isolation_level=%s", dbpath, conn.isolation_level)
    return conn

  def _load_db(self, db_name):
    db = self.dbmap[db_name] = iPhotoDB(self.iphoto, db_name)
    return db

  @locked
  def __getattr__(self, db_name):
    dbmap = self.dbmap
    if db_name in dbmap:
      return dbmap[db_name]
    dbpath = self.pathto(db_name)
    if os.path.exists(dbpath):
      return self._load_db(db_name)
    raise AttributeError(db_name)

class iPhotoDB(TableSpace):

  def __init__(self, iphoto, db_name):
    TableSpace.__init__(self, iPhotoTable, iphoto._lock, db_name=db_name)
    global SCHEMAE
    self.iphoto = iphoto
    self.dbpath = iphoto.dbpath(db_name)
    self.conn = sqlite3.connect(self.dbpath)
    self.schema = SCHEMAE[db_name]
    for nickname, schema in self.schema.items():
      self.iphoto.table_by_nickname[nickname] = iPhotoTable(self, nickname, schema)

class iPhotoTable(Table):

  def __init__(self, db, nickname, schema):
    table_name = schema['table_name']
    column_names = schema['columns']
    row_class = schema.get('mixin', iPhotoRow)
    Table.__init__(self, db, table_name, column_names=column_names, id_column='modelId', row_class=row_class)
    self.nickname = nickname
    self.schema = schema

  @prop
  def iphoto(self):
    return self.db.iphoto

  @prop
  def conn(self):
    return self.db.conn

  def update_by_column(self, upd_column, upd_value, sel_column, sel_value, sel_op='='):
    return self.update_columns((upd_column,), (upd_value,), '%s %s ?' % (sel_column, sel_op), sel_value)

  def delete_by_column(self, sel_column, sel_value, sel_op='='):
    return self.delete('%s %s ?' % (sel_column, sel_op), sel_value)

class iPhotoRow(Row):

  @prop
  def iphoto(self):
    return self._table.iphoto

  @prop
  def edit_string(self):
    return "%d:%s" % (self.modelId, self.name)

class Album_Mixin(iPhotoRow):

  @prop
  def apalbum_path(self):
    return self.iphoto.pathto(os.path.join('Database/Albums', self.uuid+'.apalbum'))

  @locked_property
  def apalbum(self):
    try:
      return AlbumPList(self.apalbum_path)
    except FileNotFoundError as e:
      error("apalbum: %r: %s", self.apalbum_path, e)
      return None

class AlbumPList(object):

  def __init__(self, plistpath):
    self.path = plistpath
    self.plist = ingest_plist(plistpath, recurse=True, resolve=True)

def FilterQuery(ifilter):
  classname = ifilter.queryClassName
  if classname == 'RKSingleItemQuery':
    return SingleItemQuery(ifilter)
  if classname == 'RKMultiItemQuery':
    return MultiItemQuery(ifilter)
  raise ValueError("unsupported filter query class name %r", classname)

class BaseFilterQuery(O):
  def __init__(self, ifilter):
    O.__init__(self, *ifilter)
class SingleItemQuery(BaseFilterQuery):
  pass
class MultiItemQuery(BaseFilterQuery):
  def __init__(self, ifilter):
    FilterQuery.__init__(self, ifilter)
    self.querySubqueries = [ FilterQuery(f) for f in self.querySubqueries ]

class Master_Mixin(iPhotoRow):

  @prop
  def pathname(self):
    return os.path.join(self.iphoto.pathto('Masters'), self.imagePath)

  @locked_property
  def versions(self):
    I = self.iphoto
    I.load_versions()
    return I.versions_by_master_id.get(self.modelId, ())

  def latest_version(self):
    vs = self.versions
    if not vs:
      raise RuntimeError("no versions for master %d: %r", self.modelId, self.pathname)
      ##return None
    return max(vs, key=lambda v: v.versionNumber)

  @prop
  def width(self):
    return self.latest_version().processedWidth

  @prop
  def height(self):
    return self.latest_version().processedHeight

  @locked_property
  def faces(self):
    I = self.iphoto
    I.load_faces()
    return I.faces_by_master_id.get(self.modelId, ())

  @locked_property
  def vfaces(self):
    I = self.iphoto
    I.load_vfaces()
    return I.vfaces_by_master_id.get(self.modelId, ())

  @locked_property
  def people(self):
    them = set()
    for vface in self.vfaces:
      who = vface.person()
      if who is not None:
        ##X("master %d + %s", self.modelId, who.name)
        them.add(who)
    return them

  @prop
  def keywords(self):
    ''' Return the keywords for the latest version of this master.
    '''
    return self.latest_version().keywords

  @prop
  def keyword_names(self):
    return [ kw.name for kw in self.keywords ]

  def Image(self):
    ''' Obtain an open Image of this master.
        Caller must close.
    '''
    return Image.open(self.pathname)

  @locked_property
  def image_info(self):
    pathname = self.pathname
    with Pfx("Image.open(%r)", pathname):
      try:
        image = self.Image()
      except OSError as e:
        error("cannot load image: %s", e)
        return None
      dx, dy = image.size
      image_info = Image_Info(dx, dy, image.format)
      image.close()
    return image_info

  @prop
  def dx(self):
    return self.image_info.dx

  @prop
  def dy(self):
    return self.image_info.dy

  @prop
  def format(self):
    return self.image_info.format

class Version_Mixin(iPhotoRow):

  @prop
  def master(self):
    master = self.iphoto.master(self.masterId)
    if master is None:
      raise ValueError("version %d masterId %d matches no master"
                       % (self.modelId, self.masterId))
    return master

  @locked_property
  def keywords(self):
    ''' Return the keywords for this version.
    '''
    return frozenset(self.iphoto.keywords_by_version(self.modelId))

  @prop
  def keyword_names(self):
    return [ kw.name for kw in self.keywords ]

class Folder_Mixin(Album_Mixin):
  pass

class Keyword_Mixin(iPhotoRow):

  def versions(self):
    ''' Return the versions with this keyword.
    '''
    I = self.iphoto
    I.load_keywordForVersions()
    for vid in I.kw4v_version_ids_by_keyword_id.get(self.modelId, ()):
      yield I.version(vid)

  def masters(self):
    ''' Return the masters with this keyword.
    '''
    ms = set()
    for version in self.versions():
      ms.add(version.master)
    return ms

  def latest_versions(self):
    ''' Return the latest version of all masters with this keyword.
    '''
    return set(master.latest_version for master in self.masters())

class Person_Mixin(iPhotoRow):

  @locked_property
  def vfaces(self):
    return set()

class VFace_Mixin(iPhotoRow):

  @prop
  def master(self):
    return self.iphoto.master(self.masterId)

  def person(self):
    if not self.isNamed:
      return None
    return self.iphoto.person(self.faceKey)

  def Image(self, padfactor=1.0):
    ''' Return an Image of this face.
    '''
    MI = self.master.Image()
    mdx, mdy = MI.size
    # convert face box into centre and radii
    rx = self.faceRectWidth / 2 * padfactor
    ry = self.faceRectHeight / 2 * padfactor
    cx = self.faceRectLeft + rx
    cy = self.faceRectTop + ry
    X("RX = %s, RY = %s, CX = %s, CY = %s", rx, ry, cx, cy)
    # Image y-ordinates are inverse of iPhoto coordinates
    ##cx = mdx - cx
    cy = 1.0 - cy
    face_box = (
                 int(mdx * (cx - rx)), int(mdy * (cy - ry)),
                 int(mdx * (cx + rx)), int(mdy * (cy + ry)),
               )
    XP("MI.size = %r, face_box = %r", MI.size, face_box)
    face_Image = MI.crop(face_box)
    face_Image.load()
    MI.close()
    return face_Image

class _SelectMasters(object):
  ''' Select masters base class.
  '''

  def select(self, masters=None):
    if masters is None:
      return self.select_from_all()
    else:
      return self.select_masters(masters)

  def select_from_all(self):
    return self.select_masters(self.iphoto.masters())

class SelectByFunction(_SelectMasters):
  ''' Select by arbitrary function on a master.
  '''
  def __init__(self, iphoto, func, invert=False):
    self.iphoto = iphoto
    self.func = func
    self.invert = invert

  def select_masters(self, masters):
    func = self.func
    invert = self.invert
    for master in masters:
      if func(master):
        if not invert:
          yield master
      elif invert:
        yield master

COMPARATORS = {
    '<':  lambda left, right: left < right,
    '<=': lambda left, right: left <= right,
    '==': lambda left, right: left == right,
    '!=': lambda left, right: left != right,
    '>=': lambda left, right: left >= right,
    '>':  lambda left, right: left > right,
}

class SelectByComparison(_SelectMasters):

  def __init__(self, iphoto, left, cmpop, right, invert):
    try:
      cmpfunc = COMPARATORS[cmpop]
    except KeyError:
      raise ValueError('unknown comparison operator %r', cmpop)
    self.iphoto = iphoto
    self.left = left
    self.cmpfunc = cmpfunc
    self.right = right
    self.cmpfunc
    self.invert = invert

  def select_masters(self, masters):
    invert = self.invert
    left = self.left
    right = self.right
    cmpfunc = self.cmpfunc
    for master in masters:
      try:
        left_val = float(left)
      except ValueError:
        try:
          left_val = int(left)
        except ValueError:
          left_val = getattr(master, left)
      try:
        right_val = float(right)
      except ValueError:
        try:
          right_val = int(right)
        except ValueError:
          right_val = getattr(master, right)
      if cmpfunc(left_val, right_val):
        if not invert:
          yield master
      else:
        if invert:
          yield master

class SelectByPerson_Name(_SelectMasters):
  ''' Select masters by person name.
  '''

  def __init__(self, iphoto, person_name, invert=False):
    self.iphoto = iphoto
    self.person_name = person_name
    self.person = iphoto.person_by_name[person_name]
    self.invert = invert

  def select_masters(self, masters):
    person = self.person
    if self.invert:
      for master in masters:
        if person not in master.people:
          yield master
    else:
      for master in masters:
        if person in master.people:
          yield master

class SelectByFilenameRE(_SelectMasters):
  ''' Select masters by regular expression.
  '''

  def __init__(self, iphoto, re_text, invert=False):
    self.iphoto = iphoto
    self.re_text = re_text
    self.invert = invert
    self.re = re.compile(re_text)

  def select_masters(self, masters):
    re = self.re
    if self.invert:
      for master in masters:
        if not re.search(master.latest_version().fileName):
          yield master
    else:
      for master in masters:
        if re.search(master.latest_version().fileName):
          yield master

class SelectByKeyword_Name(_SelectMasters):
  ''' Select masters by keyword name.
  '''

  def __init__(self, iphoto, kwname, invert=False):
    self.iphoto = iphoto
    self.kwname = kwname
    self.invert = invert

  def select_from_all(self):
    if self.invert:
      return self.select_masters(self.iphoto.masters())
    else:
      return self.iphoto.masters_by_keyword(self.kwname)

  def select_masters(self, masters):
    kwname = self.kwname
    if self.invert:
      for master in masters:
        if kwname not in master.keyword_names:
          yield master
    else:
      for master in masters:
        if kwname in master.keyword_names:
          yield master

SCHEMAE = {'Faces':
            { 'person':
                { 'table_name': 'RKFaceName',
                  'mixin': Person_Mixin,
                  'columns':
                    ( 'modelId', 'uuid', 'faceKey', 'keyVersionUuid',
                      'name', 'fullName', 'email', 'similarFacesCached',
                      'similarFacesOpen', 'manualOrder', 'lastUsedDate',
                      'attrs',
                    ),
                },
              'face':
                { 'table_name': 'RKDetectedFace',
                  'columns':
                    ( 'modelId', 'uuid', 'masterUuid', 'altMasterUuid',
                      'faceKey', 'correlatedFaceKey', 'ownerServiceKey',
                      'faceIndex', 'width', 'height', 'topLeftX',
                      'topLeftY', 'topRightX', 'topRightY', 'bottomLeftX',
                      'bottomLeftY', 'bottomRightX', 'bottomRightY',
                      'confidence', 'sharpness', 'exposureValue',
                      'rejected', 'faceCount', 'ignore', 'faceFlags',
                      'hasFaceTile', 'tileFacePosition', 'faceAngle',
                      'faceDirectionAngle', 'faceSkinScore',
                      'skippedInUnnamedFaces',
                    ),
                },
            },
           'Library':
            { 'master':
                { 'table_name': 'RKMaster',
                  'mixin': Master_Mixin,
                  'columns':
                    ( 'modelId', 'uuid', 'name', 'projectUuid', 'importGroupUuid',
                      'fileVolumeUuid', 'alternateMasterUuid', 'originalVersionUuid',
                      'originalVersionName', 'fileName', 'type', 'subtype',
                      'fileIsReference', 'isExternallyEditable', 'isTrulyRaw',
                      'isMissing', 'hasAttachments', 'hasNotes', 'hasFocusPoints',
                      'imagePath', 'fileSize', 'pixelFormat',
                      'duration', 'imageDate', 'fileCreationDate', 'fileModificationDate',
                      'imageHash', 'originalFileName', 'originalFileSize', 'imageFormat',
                      'importedBy', 'createDate', 'isInTrash', 'faceDetectionState',
                      'colorSpaceName', 'colorSpaceDefinition', 'fileAliasData',
                      'streamAssetId', 'streamSourceUuid', 'burstUuid',
                    ),
                },
              'vface':
                { 'table_name': 'RKVersionFaceContent',
                  'mixin': VFace_Mixin,
                  'columns':
                    ( 'modelId', 'versionId', 'masterId', 'isNamed', 'faceKey', 'faceIndex', 'faceRectLeft', 'faceRectTop', 'faceRectWidth', 'faceRectHeight',
                    ),
                },
              'folder':
                { 'table_name': 'RKFolder',
                  'mixin': Folder_Mixin,
                  'columns':
                    ( 'modelId', 'uuid', 'folderType', 'name', 'parentFolderUuid',
                      'implicitAlbumUuid', 'posterVersionUuid',
                      'automaticallyGenerateFullSizePreviews', 'versionCount',
                      'minImageTimeZoneName', 'maxImageTimeZoneName', 'minImageDate',
                      'maxImageDate', 'folderPath', 'createDate', 'isExpanded',
                      'isHidden', 'isHiddenWhenEmpty', 'isFavorite', 'isInTrash',
                      'isMagic', 'colorLabelIndex', 'sortAscending', 'sortKeyPath',
                    ),
                },
              'keyword':
                { 'table_name': 'RKKeyword',
                  'mixin': Keyword_Mixin,
                  'columns':
                    ( 'modelId', 'uuid', 'name', 'searchName', 'parentId',
                      'hasChildren', 'shortcut',
                    ),
                },
              'album':
                { 'table_name': 'RKAlbum',
                  'mixin': Album_Mixin,
                  'columns':
                    ( 'modelId', 'uuid', 'albumType', 'albumSubclass', 'serviceName',
                      'serviceAccountName', 'serviceFullName', 'name', 'folderUuid',
                      'queryFolderUuid', 'posterVersionUuid', 'selectedTrackPathUuid',
                      'sortKeyPath', 'sortAscending', 'customSortAvailable',
                      'versionCount', 'createDate', 'isFavorite', 'isInTrash', 'isHidden',
                      'isMagic', 'publishSyncNeeded', 'colorLabelIndex',
                      'faceSortKeyPath', 'recentUserChangeDate', 'filterData',
                      'queryData', 'viewData', 'selectedVersionIds',
                    ),
                },
              'version':
                { 'table_name': 'RKVersion',
                  'mixin': Version_Mixin,
                  'columns':
                    ( 'modelId', 'uuid', 'name', 'fileName',
                      'versionNumber', 'stackUuid', 'masterUuid',
                      'masterId', 'rawMasterUuid', 'nonRawMasterUuid',
                      'projectUuid', 'imageTimeZoneName', 'imageDate',
                      'mainRating', 'isHidden', 'isFlagged', 'isOriginal',
                      'isEditable', 'colorLabelIndex', 'masterHeight',
                      'masterWidth', 'processedHeight', 'processedWidth',
                      'rotation', 'hasAdjustments', 'hasEnabledAdjustments',
                      'hasNotes', 'createDate', 'exportImageChangeDate',
                      'exportMetadataChangeDate', 'isInTrash',
                      'thumbnailGroup', 'overridePlaceId', 'exifLatitude',
                      'exifLongitude', 'renderVersion', 'adjSeqNum',
                      'supportedStatus', 'videoInPoint', 'videoOutPoint',
                      'videoPosterFramePoint', 'showInLibrary',
                      'editState', 'contentVersion', 'propertiesVersion',
                      'rawVersion', 'faceDetectionIsFromPreview',
                      'faceDetectionRotationFromMaster', 'editListData',
                      'hasKeywords',
                    ),
                },
              'keywordForVersion':
                { 'table_name': 'RKKeywordForVersion',
                  'columns':
                    ( 'modelId', 'versionId', 'keywordId'),
                },
            }
          }

if __name__ == '__main__':
  sys.exit(main(sys.argv))
