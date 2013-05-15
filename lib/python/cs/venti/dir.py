import os
import os.path
import pwd
import grp
import stat
import sys
from threading import Lock
from cs.logutils import Pfx, debug, error, info, warning
from cs.venti import totext, fromtext
from cs.lex import hexify
from cs.seq import seq
from cs.serialise import get_bs, put_bs
from cs.threads import locked_property
from .block import decodeBlock
from .blockify import blockFromString
from .meta import Meta

uid_nobody = -1
gid_nogroup = -1

# Directories (Dir, a subclass of dict) and directory entries (Dirent).

D_FILE_T = 0
D_DIR_T = 1
def D_type2str(type_):
  if type_ == D_FILE_T:
    return "D_FILE_T"
  if type_ == D_DIR_T:
    return "D_DIR_T"
  return str(type_)

F_HASMETA = 0x01
F_HASNAME = 0x02

def decodeDirent(data, offset):
  ''' Unserialise a Dirent, return (dirent, offset).
      Input format: bs(type)bs(flags)[bs(metalen)meta][bs(namelen)name]block
  '''
  type_, offset = get_bs(data, offset)
  flags, offset = get_bs(data, offset)
  meta = None
  if flags & F_HASMETA:
    metalen, offset = get_bs(data, offset)
    if metalen >= len(s):
      raise ValueError("metalen %d >= len(s) %d" % (metalen, len(s)))
    meta = s[:metalen]
    s = s[metalen:]
  meta = Meta(meta)
  if flags & F_HASNAME:
    namelen, offset = get_bs(data, offset)
    if namelen >= len(s):
      raise ValueError("namelen %d >= len(s) %d" % (namelen, len(s)))
    name = s[:namelen]
    s = s[namelen:]
  else:
    name = ""
  block, s = decodeBlock(s)
  if type_ == D_DIR_T:
    E = Dir(name, meta=meta, parent=None, content=block)
  elif type_ == D_FILE_T:
    E = FileDirent(name, meta=meta, block=block)
  else:
    E = _BasicDirent(type_, name, meta, block)
  return E, offset

def decodeDirents(dirdata, offset=0):
  ''' Yield Dirents from the supplied bytes `dirdata`.
  '''
  while offset < len(dirdata):
    E, offset = decodeDirent(dirdata, offset)
    if E.name is None or len(E.name) == 0:
      # FIXME: skip unnamed dirent
      warning("skip unnamed Dirent")
      continue
    if E.name == '.' or E.name == '..':
      continue
    yield E

class Dirent(object):
  ''' Incomplete base class for Dirent objects.
  '''

  def __init__(self, type_, name, meta=None):
    if not isinstance(type_, int):
      raise TypeError("type_ is not an int: <%s>%r" % (type(type_), type_))
    if name is not None and not isinstance(name, str):
      raise TypeError("name is neither None nor str: <%s>%r" % (type(name), name))
    if meta is None:
      meta = Meta()
    else:
      if not isinstance(meta, Meta):
        raise TypeError("meta is not a Meta: <%s>%r" % (type(meta), meta))
    self.type = type_
    self.name = name
    self.meta = meta
    self.d_ino = None

  def __str__(self):
    return self.textencode()

  def __repr__(self):
    return "Dirent(%s, %s, %s)" % (D_type2str, self.name, self.meta)

  @property
  def isfile(self):
    ''' Is this a file Dirent?
    '''
    return self.type == D_FILE_T

  @property
  def isdir(self):
    ''' Is this a directory Dirent?
    '''
    return self.type == D_DIR_T

  def updateFromStat(self, st):
    self.meta.updateFromStat(st)

  def encode(self, noname=False):
    ''' Serialise the dirent.
        Output format: bs(type)bs(flags)[bs(metalen)meta][bs(namelen)name]block
    '''
    flags = 0

    meta = self.meta
    if meta:
      if not isinstance(meta, Meta):
        raise TypeError("self.meta is not a Meta: <%s>%r" % (type(meta), meta))
      metatxt = meta.encode()
      if len(metatxt) > 0:
        metatxt = toBS(len(metatxt))+metatxt
        flags |= F_HASMETA
    else:
      metatxt = ""

    name = self.name
    if noname:
      name = ""
    elif name is not None and len(name) > 0:
      name = toBS(len(name))+name
      flags |= F_HASNAME
    else:
      name = ""

    block = self.getBlock()
    return toBS(self.type) \
         + toBS(flags) \
         + metatxt \
         + name \
         + block.encode()

  def textencode(self):
    ''' Serialise the dirent as text.
        Output format: bs(type)bs(flags)[bs(metalen)meta][bs(namelen)name]block
    '''
    flags = 0

    meta = self.meta
    if meta:
      if not isinstance(meta, Meta):
        raise TypeError("self.meta is not a Meta: <%s>%r" % (type(meta), meta))
      metatxt = meta.encode()
      if len(metatxt) > 0:
        metatxt = hexify(toBS(len(metatxt))) + totext(metatxt)
        flags |= F_HASMETA
    else:
      metatxt = ""

    name = self.name
    if name is None or len(name) == 0:
      nametxt = ""
    else:
      nametxt = hexify(toBS(len(name))) + totext(name)
      flags |= F_HASNAME

    block = self.getBlock()
    return ( hexify(toBS(self.type))
           + hexify(toBS(flags))
           + metatxt
           + nametxt
           + block.textencode()
           )

  # TODO: make size a property?
  def size(self):
    return len(self.getBlock())

  @property
  def mtime(self):
    return self.meta.mtime
  @mtime.setter
  def mtime(self, newtime):
    self.meta.mtime = newtime

  def stat(self):
    from pwd import getpwnam
    meta = self.meta
    user, group, unixmode = meta.unixPerms()
    if user is None:
      uid = uid_nobody
    else:
      try:
        uid = getpwnam(user)[2]
      except KeyError:
        uid = uid_nobody

    if group is None:
      gid = gid_nogroup
    else:
      try:
        gid = getpwnam(user)[2]
      except KeyError:
        gid = gid_nogroup

    if self.type == D_DIR_T:
      unixmode |= stat.S_IFDIR
    else:
      unixmode |= stat.S_IFREG

    if self.d_ino is None:
      self.d_ino = seq()
    ino = self.d_ino

    dev = 0       # FIXME: we're not hooked to a FS?
    nlink = 1
    size = self.size()
    atime = 0
    mtime = self.mtime
    ctime = 0

    return (unixmode, ino, dev, nlink, uid, gid, size, atime, mtime, ctime)

class _BasicDirent(Dirent):
  ''' A _BasicDirent represents a file or directory in the store.
  '''
  def __init__(self, type_, name, meta, block):
    Dirent.__init__(self, type_, name, meta)
    self.__block = block

  def getBlock(self):
    return self.__block

  def __getitem__(self, name):
    if self.isdir:
      return self.asdir()[name]
    raise KeyError("\"%s\" not in %s" % (name, self))

def FileDirent(name, meta, block):
  ''' Factory function to return a Dirent for a file.
      Parameters:
        `name`: the file name to store in the Dirent.
        `meta`: the file meta data; may be None.
        `block`: the top block of the file content.
  '''
  return _BasicDirent(D_FILE_T, name, meta, block)

class FileDirent(_BasicDirent):

  def __init__(self, name, meta, block):
    _BasicDirent.__init__(self, D_FILE_T, name, meta, block)

  def restore(self, path, makedirs=False, verbosefp=None):
    ''' Restore this Dirent's file content to the name `path`.
    '''
    with Pfx("FileDirent.restore(%s)", path):
      if verbosefp is not None:
        verbosefp.write(path)
        verbosefp.write('\n')
      dirpath = os.path.dirname(path)
      if len(dirpath) and not os.path.isdir(dirpath):
        if makedirs:
          os.makedirs(dirpath)
      with open(path, "wb") as ofp:
        for B in self.getBlock().leaves():
          ofp.write(B.blockdata())
        fd = ofp.fileno()
        st = os.fstat(fd)
        user, group, perms = self.meta.unixPerms()
        if user is not None or group is not None:
          os.fchmod(fd, perms)
        if user is None:
          uid = -1
        else:
          uid = pwd.getpwnam(user)[2]
          if uid == st.st_uid:
            uid = -1
        if group is None:
          gid = -1
        else:
          gid = grp.getgrnam(group)[2]
          if gid == st.st_gid:
            gid = -1
        if uid != -1 or gid != -1:
          os.fchown(fd, uid, gid)
      if self.meta.mtime is not None:
        os.utime(path, (st.st_atime, self.meta.mtime))

class Dir(Dirent):
  ''' A directory.
  '''

  def __init__(self, name, meta=None, parent=None, dirblock=None):
    ''' Initialise this directory.
        `meta`: meta information
        `parent`: parent Dir
        `dirblock`: pre-existing Block with initial Dir content
    '''
    self._lock = Lock()
    if meta is None:
      meta = Meta()
    Dirent.__init__(self, D_DIR_T, name, meta)
    self.parent = parent
    self.entries = {}
    if dirblock:
      for E in decodeDirents(dirblock.data):
        E.parent = self
        self[E.name] = E
    self._lock = Lock()

  def dirs(self):
    return [ name for name in self.keys() if self[name].isdir ]

  def files(self):
    return [ name for name in self.keys() if self[name].isfile ]

  def _validname(self, name):
    return len(name) > 0 and name.find('/') < 0

  def get(self, name, dflt=None):
    if name not in self:
      return dflt
    return self[name]

  def keys(self):
    return self.entries.keys()

  def __contains__(self, name):
    if name == '.':
      return True
    if name == '..':
      return self.parent is not None
    return name in self.entries

  def __iter__(self):
    return self.keys()

  def __getitem__(self, name):
    if name == '.':
      return self
    if name == '..':
      return self.parent
    return self.entries[name]

  def __setitem__(self, name, E):
    ''' Store a Dirent in the specified name slot.
    '''
    ##debug("<%s>[%s]=%s" % (self.name, name, E))
    if not self._validname(name):
      raise KeyError("invalid name: %s" % (name,))
    if name in self:
      raise KeyError("name already present: %s" % (name,))
    if not isinstance(E, Dirent):
      raise ValueError("E is not a Dirent: <%s>%r" % (type(E), E))
    self.entries[name] = E

  def __delitem__(self, name):
    if not self._validname(name):
      raise KeyError("invalid name: %s" % (name,))
    if name == '.' or name == '..':
      raise KeyError("refusing to delete . or ..: name=%s" % (name,))
    del self.entries[name]

  def getBlock(self):
    ''' Return the top Block referring to an encoding of this Dir.
    '''
    names = self.keys()
    names.sort()
    return blockFromString(
            "".join( self[name].encode()
                     for name in names
                     if name != '.' and name != '..'
                   ))

  def rename(self, oldname, newname):
    ''' Rename entry `oldname` to entry `newname`.
    '''
    E = self[oldname]
    del E[oldname]
    E.name = newname
    self[newname] = E

  def open(self, name):
    ''' Open the entry named `name` as a readable file-like object.
    '''
    from .file import ReadFile
    return ReadFile(self[name].getBlock())

  def mkdir(self, name):
    ''' Create a subdirectory named `name`, return the Dirent.
    '''
    debug("<%s>.mkdir(%s)..." % (self.name, name))
    D = self[name] = Dir(name, parent=self)
    return D

  def chdir1(self, name):
    ''' Change directory to the immediate entry `name`.
        Return the entry.
    '''
    D = self[name]
    if not D.isdir:
      raise ValueError("%s[name=%s]: not a directory" % (self, name))
    return D

  def chdir(self, path):
    ''' Change directory to `path`, return the ending directory.
    '''
    D = self
    for name in path.split('/'):
      if len(name) == 0:
        continue
      D = D.chdir1(name)
    return D

  def makedirs(self, path):
    ''' Like os.makedirs(), create a directory path at need.
        Returns the bottom directory.
    '''
    D = self
    for name in path.split('/'):
      if len(name) == 0:
        continue
      if name == '.':
        continue
      if name == '..':
        D = D.parent
        continue
      E = D.get(name)
      if E is None:
        E = D.mkdir(name)
      else:
        if not E.isdir:
          raise ValueError("%s[name=%s] is not a directory" % (D, name))
      D = E
    return D

  def storeFilename(self, filepath, filename,
                trust_size_mtime=False, ignore_existing=False):
    ''' Store as `filename` to file named by `filepath`.
    '''
    import  cs.venti.file
    with Pfx("%s.storeFile(%s, %s, trust_size_mtime=%s, ignore_existing=%s",
             self, filename, filepath, trust_size_mtime, ignore_existing):
      E = self.get(filename)
      if ignore_existing and E is not None:
        debug("already exists, skipping")
        return E

      if trust_size_mtime and E is not None and E.isfile:
        st = os.stat(filepath)
        if st.st_size == E.size() and int(st.st_mtime) == int(E.mtime):
          debug("same size and mtime, skipping")
          return E
        debug("differing size(%s:%s)/mtime(%s:%s)",
              st.st_size, E.size(),
              int(st.st_mtime), int(E.mtime))

      if E is None or not E.isfile:
        matchBlocks = None
      else:
        matchBlocks = E.getBlock().leaves()

      E = cs.venti.file.storeFilename(filepath, filename, matchBlocks=matchBlocks)
      if filename in self:
        del self[filename]
      self[filename] = E
    return E
