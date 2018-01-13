#!/usr/bin/python

''' Archive files.

    Archive files are records of data saved to a Store.
    Lines are appended to the archive file of the form:

      isodatetime unixtime dirent

    where unixtime is UNIX time (seconds since epoch) and dirent is the text
    encoding of a Dirent.
'''

from __future__ import print_function
import os
from os.path import realpath
import stat
import time
from datetime import datetime
import errno
from itertools import chain
from cs.fileutils import lockfile, shortpath
from cs.inttypes import Flags
from cs.lex import unctrl
from cs.logutils import info, warning, error
from cs.pfx import Pfx
from cs.py.func import prop
from cs.x import X
from .blockify import blockify, top_block_for
from .dir import FileDirent, DirFTP
from .file import filedata
from .paths import decode_Dirent_text, resolve, walk

CopyModes = Flags('delete', 'do_mkdir', 'trust_size_mtime')

# shared mapping of archive paths to Archive instances
_ARCHIVES = {}

def Archive(path, mapping=None):
  ''' Return an Archive for the named file.
      Maintains a mapping of issues Archives in order to reuse that
      same Archive for a given path.
  '''
  global _ARCHIVES
  if not path.endswith('.vt'):
    warning("unusual Archive path: %r", path)
  if mapping is None:
    mapping = _ARCHIVES
  path = realpath(path)
  A = mapping.get(path)
  if A is None:
    mapping[path] = A = _Archive(path)
  return A

class _Archive(object):
  ''' Manager for an archive.vt file.
  '''

  def __init__(self, arpath):
    self.path = arpath
    self._entries__filename = arpath

  def __str__(self):
    return "Archive(%s)" % (shortpath(self.path),)

  @prop
  def last(self):
    ''' Return the last (unixtime, Dirent) from the file, or (None, None).
    '''
    entry = None, None
    for entry in self:
      pass
    return entry

  def __iter__(self):
    ''' Generator yielding (unixtime, Dirent) from the archive file.
    '''
    path = self.path
    # The funny control structure is because we're using Pfx in a generator.
    # It needs to be set up and torn down between yields.
    with Pfx(path):
      try:
        fp = open(path)
      except OSError as e:
        if e.errno != errno.ENOENT:
          raise
    entries = self.parse(fp)
    while True:
      with Pfx(path):
        when, E = next(entries)
        if when is None and E is None:
          break
      # note: yield _outside_ Pfx
      yield when, E

  def save(self, E, when=None):
    ''' Save the supplied Dirent `E` with timestamp `when` (default now); returns the text form of `E`.
    '''
    path = self.path
    with lockfile(path):
      with open(path, "a") as fp:
        return self.write(fp, E, when=when, etc=E.name)

  @staticmethod
  def write(fp, E, when=None, etc=None):
    ''' Write a Dirent to an open archive file; return the textual form of `E` used.
        Archive lines have the form:
          isodatetime unixtime totext(dirent) dirent.name
       Note: does not flush the file.
    '''
    encoded = _Archive.strfor_Dirent(E)
    if when is None:
      when = time.time()
    # produce a local time with knowledge of its timezone offset
    dt = datetime.fromtimestamp(when).astimezone()
    assert dt.tzinfo is not None
    fp.write(dt.isoformat())
    fp.write(' ')
    fp.write(str(when))
    fp.write(' ')
    fp.write(encoded)
    if etc is not None:
      fp.write(' ')
      fp.write(unctrl(etc))
    fp.write('\n')
    return encoded

  @staticmethod
  def parse(fp, first_lineno=1):
    ''' Parse lines from an open archive file, yield (when, E).
    '''
    for lineno, line in enumerate(fp, first_lineno):
      with Pfx(str(lineno)):
        if not line.endswith('\n'):
          raise ValueError("incomplete? no trailing newline")
        line = line.strip()
        if not line or line.startswith('#'):
          continue
        # allow optional trailing text, which will be the E.name part normally
        fields = line.split(None, 3)
        isodate, unixtime, dent = fields[:3]
        when = float(unixtime)
        E = decode_Dirent_text(dent)
        info("when=%s, E=%s", when, E)
      # note: yield _outside_ Pfx
      yield when, E

  @staticmethod
  def read(fp, first_lineno=1):
    ''' Read the next entry from an open archive file, return (when, E).
        Return (None, None) at EOF.
    '''
    for when, E in _Archive._parselines(fp, first_lineno=first_lineno):
      return when, E
    return None, None

  @staticmethod
  def strfor_Dirent(E):
    ''' Exposed function for obtaining the text form of a Dirent.
        This is to support callers optimising away calls to .save
        if they know the previous save state, obtainable from this
        function.
    '''
    return E.textencode()

def copy_in_dir(rootD, rootpath, modes, log=None):
  ''' Copy the os directory tree at `rootpath` over the Dir `rootD`.
      `modes` is an optional CopyModes value.
  '''
  with Pfx("copy_in(%s)", rootpath):
    if not os.path.isdir(rootpath):
      warning("not a directory?")
    rootpath_prefix = rootpath + '/'
    # TODO: try out scandir sometime
    for ospath, dirnames, filenames in os.walk(rootpath):
      with Pfx(ospath):
        dst = os.lstat(ospath)
        if not stat.S_ISDIR(dst.st_mode):
          warning("not a directory? SKIPPED")
          continue

        # get matching Dir from store
        if ospath == rootpath:
          relpath = '.'
          dirD = rootD
        elif ospath.startswith(rootpath_prefix):
          relpath = ospath[len(rootpath_prefix):]
          dirD, parent, subpaths = resolve(rootD, ospath[len(rootpath_prefix):])
          for name in subpaths:
            E = dirD.get(name)
            if E is None:
              dirD = dirD.chdir1(name)
            elif E.isdir:
              warning("surprise: resolve stopped at a subdir! subpaths=%r", subpaths)
              dirD = E
            else:
              del dirD[name]
              dirD = dirD.chdir1(name)
        else:
          warning("unexpected ospath, SKIPPED")
          continue

        if modes.delete:
          # Remove entries in dirD not present in the real filesystem
          allnames = set(dirnames)
          allnames.update(filenames)
          Dnames = sorted(dirD.keys())
          for name in Dnames:
            if name not in allnames:
              relsubname = os.path.join(relpath, name)
              log("delete %s", relsubname)
              del dirD[name]

        for dirname in sorted(dirnames):
          with Pfx("%s/", dirname):
            reldirname = os.path.join(relpath, dirname)
            if dirname not in dirD:
              log("mkdir  %s", reldirname)
              dirD.mkdir(dirname)
            else:
              E = dirD[dirname]
              if not E.isdir:
                # old name is not a dir - toss it and make a dir
                log("replace/mkdir %s", reldirname)
                del dirD[dirname]
                E = dirD.mkdir(dirname)

        for filename in sorted(filenames):
          with Pfx(filename):
            relfilename = os.path.join(relpath, filename)
            ##if modes.ignore_existing and filename in dirD:
            ##  log("skip     %s (already in archive)", relfilename)
            ##  continue
            filepath = os.path.join(ospath, filename)
            if not os.path.isfile(filepath):
              log("skip     %s (not regular file: %s)", relfilename, filepath)
              continue
            if filename not in dirD:
              fileE = FileDirent(filename)
              dirD[filename] = fileE
            else:
              fileE = dirD[filename]
              if modes.trust_size_mtime:
                B = fileE.block
                M = fileE.meta
                fst = os.stat(filepath)
                if fst.st_mtime == M.mtime and fst.st_size == B.span:
                  log("skipfile %s (same mtime/size)", relfilename)
                  continue
                else:
                  error("DIFFERING size/mtime: B.span=%d/M.mtime=%s VS st_size=%d/st_mtime=%s",
                        B.span, M.mtime, fst.st_size, fst.st_mtime)
            log("file     %s", relfilename)
            try:
              copy_in_file(fileE, filepath, modes)
            except OSError as e:
              error(str(e))
              continue
            except IOError as e:
              error(str(e))
              continue

        # finally, update the Dir meta info
        dirD.meta.update_from_stat(dst)

def copy_in_file(E, filepath, modes, log=None):
  ''' Store the file named `filepath` over the FileDirent `E`.
  '''
  with Pfx(filepath):
    with open(filepath, "rb") as fp:
      B = E.block = top_block_for(_blockify_file(fp, E))
      st = os.fstat(fp.fileno())
    if B.span != st.st_size:
      warning("MISMATCH: B.span=%d, st_size=%d", B.span, st.st_size)
      filedata = open(filepath, "rb").read()
      blockdata = B.all_data()
      X("len(filedata)=%d", len(filedata))
      X("len(blockdata)=%d", len(blockdata))
      open("data1file", "wb").write(filedata)
      open("data2block", "wb").write(blockdata)
      raise RuntimeError("ABORT")
    E.meta.update_from_stat(st)

def _blockify_file(fp, E):
  ''' Read data from the file `fp` and compare with the FileDirect `E`, yielding leaf Blocks for a new file.
      This underpins copy_in_file().
  '''
  # read file data in chunks matching the existing leaves
  # return the leaves while the data match
  data = None
  for B in E.block.leaves:
    data = fp.read(len(B))
    if len(data) == 0:
      # EOF
      return
    if len(data) != len(B):
      break
    if not B.matches_data(data):
      break
    data = None
    yield B
  # blockify the remaining file data
  chunks = filedata(fp)
  if data is not None:
    chunks = chain( [data], chunks )
  for B in blockify(chunks):
    yield B

def copy_out_dir(rootD, rootpath, modes=None, log=None):
  ''' Copy the Dir `rootD` onto the os directory `rootpath`.
      `modes` is an optional CopyModes value.
      Notes: `modes.delete` not implemented.
  '''
  if modes is None:
    modes = CopyModes()
  with Pfx("copy_out(rootpath=%s)", rootpath):
    for thisD, relpath, dirs, files in walk(rootD, topdown=True):
      if relpath:
        dirpath = os.path.join(rootpath, relpath)
      else:
        dirpath = rootpath
      with Pfx(dirpath):
        if not os.path.isdir(dirpath):
          if modes.do_mkdir:
            log("mkdir   %s", dirpath)
            try:
              os.mkdir(dirpath)
            except OSError as e:
              error("mkdir: %s", e)
              dirs[:] = ()
              files[:] = ()
              continue
          else:
            error("refusing to mkdir, not requested")
            dirs[:] = ()
            files[:] = ()
            continue
        # apply the metadata now in case of setgid etc
        thisD.meta.apply_posix(dirpath)
        for filename in sorted(files):
          with Pfx(filename):
            E = thisD[filename]
            if not E.isfile:
              warning("vt source is not a file, skipping")
              continue
            filepath = os.path.join(dirpath, filename)
            copy_out_file(E, filepath, modes, log=log)
        # apply the metadata again
        thisD.meta.apply_posix(dirpath)

def copy_out_file(E, ospath, modes=None, log=None):
  ''' Update the OS file `ospath` from the FileDirent `E` according to `modes`.
  '''
  if modes is None:
    modes = CopyModes()
  if not E.isfile:
    raise ValueError("expected FileDirent, got: %r" % (E,))
  try:
    # TODO: should this be os.stat if we don't support symlinks?
    st = os.lstat(ospath)
  except OSError as e:
    st = None
  else:
    if modes.ignore_existing:
      X("already exists, ignoring")
      return
  B = E.block
  M = E.meta
  if ( modes.trust_size_mtime
   and st is not None
   and ( M.mtime is not None and M.mtime == st.st_mtime
         and B.span == st.st_size
       )
     ):
    log("skip  %s (same size/mtime)", ospath)
    return
  # create or overwrite the file
  # TODO: backup mode in case of write errors?
  with Pfx(ospath):
    Blen = len(B)
    log("rewrite %s", ospath)
    with open(ospath, "wb") as fp:
      wrote = 0
      for chunk in B.chunks:
        fp.write(chunk)
        wrote += len(chunk)
    if Blen != wrote:
      error("Block len = %d, wrote %d", Blen, wrote)
    M.apply_posix(ospath)

class ArchiveFTP(DirFTP):

  def __init__(self, arpath, prompt=None):
    self.path = arpath
    self.archive = Archive(arpath)
    if prompt is None:
      prompt = shortpath(arpath)
    when, rootD = self.archive.last
    self.rootD = rootD
    super().__init__(rootD, prompt=prompt)

  def postloop(self):
    super().postloop()
    self.archive.save(self.rootD)
