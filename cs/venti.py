#!/usr/bin/python

''' A data store patterned after the Venti scheme:
      http://library.pantek.com/general/plan9.documents/venti/venti.html
    but supporting variable sized blocks.
    See also the Plan 9 Venti support manual pages:
      http://swtch.com/plan9port/man/man7/venti.html
    and the Wikipedia entry:
      http://en.wikipedia.org/wiki/Venti

    TODO:
      Compress the data chunks in the data file.
      MetaFS interface using real files for metadata and their contents
	as hashes.
'''

import os.path
from zlib import compress, decompress
## NOTE: migrate to hashlib sometime when python 2.5 more common
import sha
from cs.misc import warn, progress, verbose
import cs.hier
from cs.lex import unctrl

def hash_sha(block):
  ''' Returns the SHA-1 checksum for the supplied block.
  '''
  hash=sha.new(block)
  return hash.digest()

def unhex(hexstr):
  ''' Return raw byte array from hexadecimal string.
  '''
  return "".join([chr(int(hexstr[i:i+2],16)) for i in range(0,len(hexstr),2)])

def genHex(data):
  for c in data:
    yield '%02x'%ord(c)

def hex(data):
  return "".join(genHex(data))

def writehex(fp,data):
  ''' Write data in hex to file.
  '''
  for w in genHex(data):
    fp.write(w)

class RawStore(dict):
  def __init__(self,path):
    dict.__init__(self)
    self.__path=path
    progress("indexing", path)
    self.__fp=open(path,"a+b")
    self.loadIndex()

  def __setitem__(self,key,value):
    raise IndexError

  def __getitem__(self,key):
    v=dict.__getitem__(self,key)
    if v[2] is None:
      self.__fp.seek(v[0])
      v[2]=decompress(self.__fp.read(v[1]))
    return v[2]

  def hash(self,block):
    ''' Compute the hash for a block.
    '''
    return hash_sha(block)

  def store(self,block):
    h=self.hash(block)
    if h in self:
      verbose(self.__path,"already contains",hex(h))
    else:
      zblock=compress(block)
      self.__fp.seek(0,2)
      self.__fp.write(str(len(zblock)))
      self.__fp.write("\n")
      offset=self.__fp.tell()
      self.__fp.write(zblock)
      dict.__setitem__(self,h,[offset,len(zblock),block])

    return h

  def loadIndex(self):
    fp=self.__fp
    fp.seek(0)
    while True:
      e=self.__loadEntryHead(fp)
      if e is None:
        break
      (offset, zsize)=e
      zblock=fp.read(zsize)
      block=decompress(zblock)
      h=self.hash(block)
      dict.__setitem__(self,h,[offset,zsize,None])

  def __loadEntryHead(self,fp,offset=None):
    if offset is not None:
      fp.seek(offset)
    line=fp.readline()
    if len(line) == 0:
      return None
    assert len(line) > 1 and line[-1] == '\n'
    number=line[:-1]
    assert number.isdigit()
    return (fp.tell(), int(number))

class Store(RawStore):
  pass

MAX_SUBBLOCKS=16

class BlockList:
  def __init__(self,S,h=None):
    self.__store=S
    self.__blocks=[]
    if h is not None:
      print "load iblock: h =", hex(h)
      iblock=S[h]
      while len(iblock) > 0:
        isIndirect=bool(ord(iblock[0]))
        hlen=ord(iblock[1])
        h=iblock[2:2+hlen]
        print "load: indir = %s (%d) %s" %(`isIndirect`, ord(iblock[0]), hex(h))
        self.append(h,isIndirect)
        iblock=iblock[2+hlen:]

  def __len__(self):
    return len(self.__blocks)

  def append(self,h,isIndirect):
    self.__blocks.append((isIndirect,h))

  def __getitem__(self,i):
    return self.__blocks[i]

  def pack(self):
    ##warn("pack", cs.hier.h2a(self.__blocks))
    return "".join([chr(int(sb[0]))+chr(len(sb[1]))+sb[1] for sb in self.__blocks])

  def __iter__(self):
    S=self.__store
    for (isIndirect,h) in self.__blocks:
      if isIndirect:
        for subblock in BlockList(S,h):
          yield subblock
      else:
        yield S[h]

class BlockSink:
  def __init__(self,S):
    self.__store=S
    self.__lists=[BlockList(S)]
  
  def append(self,block,isIndirect=False):
    S=self.__store
    level=0
    h0=h=S.store(block)
    while True:
      bl=self.__lists[level]
      ##print "append: bl =", `bl`
      if len(bl) < MAX_SUBBLOCKS:
        bl.append(h,isIndirect)
        return h0

      # pack up full block for storage at next indirection level
      fullblock=bl.pack()
      # prepare fresh empty block at this level
      bl=self.__lists[level]=BlockList(S)
      # store the current block in the fresh indirect block
      bl.append(h,isIndirect)
      # advance to the next level of indirection and repeat
      # to store the full indirect block
      level+=1
      isIndirect=True
      block=fullblock
      h=S.store(block)
      if level == len(self.__lists):
        print "new indir level", level
        self.__lists.append(BlockList(S))

  def close(self):
    ''' Store all the outstanding indirect blocks.
        Return the hash of the top level block.
    '''
    S=self.__store
    # record the lower level indirect blocks in their parents
    while len(self.__lists) > 1:
      bl=self.__lists.pop(0)
      self.append(bl.pack(),True)

    # stash and return the topmost indirect block
    h=S.store(self.__lists[0].pack())
    self.__lists=None
    return h

class _Sink:
  ''' A File-like class that supplies only write, close, flush.
      Returned by the sink() method of Store.
      Write(), close() and flush() return the hash code for flushed data.
      Write() usually returns None unless too much is queued; then it
      flushes some data and returns the hash.
      Flush() may return None if there is no queued data.
  '''
  def __init__(self,store):
    ''' Arguments: fp, the writable File object to encapsulate.
                   store, a cs.venti.Store compatible object.
    '''
    self.__store=store
    self.__q=''

  def close(self):
    ''' Close the file. Return the hash code of the unreported data.
    '''
    return self.flush()

  def write(self,data):
    ''' Queue data for writing.
    '''
    self.__q+=data
    return None

  def flush(self):
    ''' Like flush(), but returns the hash code of the data since the last
        flush. It may return None if there is nothing to flush.
    '''
    if len(self.__q) == 0:
      return None

    hash=self.__store.store(self.__q)
    self.__q=''
    return hash

class SaveFile:
  ''' A File-like class for serialising data to the store as a "file".
      It only supports write() and close().
      close() returns the hash of the top indirect block.
  ''' 
  def __init__(self,store,findEdge=None):
    if findEdge is None: findEdge=self.__findEdge
    self.__store=store
    self.__findEdge=self.__dfltFindEdge
    self.__q=''
  def __dfltFindEdge(self,buf):
    if len(buf) >= DFLT_BUF:
      return DFLT_BUF
    return -1
  def NO_FIND_EDGE(self):
    ''' Edge function to completely disable automatic edge finding.
    '''
    return -1
  def write(self,data):
    ''' Queue data for the store.
    '''
    self.__q+=data
    edge=self.__findEdge(self.__q)
    assert edge != 0, "edge function should never return 0; return -1 for 'no edge'"
    if edge >= 0:
      newhash=self.__store.store(self.__q[:edge])
      self.__appendHash(newhash)
      self.__q=self.__q[edge:]

def stashCode(store,fp):
  ''' Save C-like or Python-like code to the block store as a file.
      Returns the hash to the top indirect block.
  '''
  S=store.fileSink()
  for line in fp:
    if line[:4] == "def " or line[:6] == "class ":
      S.flush()
    S.write(line)
    if line[0] == "}":
      S.flush()
  return S.close()
