import os
import os.path
import time
import socket
import email.Parser
import string
import StringIO
import re
from cs.misc import warn, progress, verbose, seq, saferename

def ismhdir(path):
  return os.path.isfile(os.path.join(path,'.mh_sequences'))

def ismaildir(path):
  for subdir in ('new','cur','tmp'):
    if not os.path.isdir(os.path.join(path,subdir)):
      return False
  return True

def maildirify(path):
  for subdir in ('new','cur','tmp'):
    dpath=os.path.join(path,subdir)
    if not os.path.isdir(dpath):
      os.makedirs(dpath)

_delivered=0
def nextDelivered():
  global _delivered
  _delivered+=1
  return _delivered

_MaildirInfo_RE = re.compile(r':(\d+,[^/]*)$')

class Maildir:
  def __init__(self,path):
    self.__path=path
    self.__parser=email.Parser.Parser()
    self.__hostname=None

  def mkbasename(self):
    now=time.time()
    secs=int(now)
    subsecs=now-secs

    left=str(secs)
    if self.__hostname is None:
      self.__hostname=socket.gethostname()
    right=self.__hostname.replace('/','\057').replace(':','\072')
    middle='#'+str(seq())+'M'+str(subsecs*1e6)+'P'+str(os.getpid())+'Q'+str(nextDelivered())

    return string.join((left,middle,right),'.')

  def mkname(self,info=None):
    name=self.mkbasename()
    if info is None:
      return os.path.join('new',name)
    return os.path.join('cur',name+":"+info)

  def keys(self):
    return self.subpaths()

  def subpaths(self):
    for subdir in ('new','cur'):
      subpath=os.path.join(self.__path,subdir)
      for name in os.listdir(subpath):
        if len(name) > 0 and name[0] != '.':
	  yield os.path.join(subdir,name)

  def fullpath(self,subpath):
    return os.path.join(self.__path,subpath)

  def paths(self):
    for subpath in self.subpaths():
      yield self.fullpath(subpath)

  def __iter__(self):
    P=email.Parser.Parser()
    for subpath in self.subpaths():
      yield self[subpath]

  def __getitem__(self,subpath):
    return self.__parser.parse(file(self.fullpath(subpath)))

  def newItem(self):
    return MaildirNewItem(self)

  def headers(self,subpath):
    fp=file(self.fullpath(subpath))
    headertext=''
    for line in fp:
      headertext+=line
      if len(line) == 0 or line == "\n":
        break

    fp=StringIO.StringIO(headertext)
    return self.__parser.parse(fp, headersonly=True)

  def importPath(self,path):
    info=None
    m=_MaildirInfo_RE.search(path)
    if m:
      info=m.group(1)

    newname=self.fullpath(self.mkname(info))
    progress(path, '=>', newname)
    saferename(path,newname)

class MaildirNewItem:
  def __init__(self,maildir):
    self.__maildir=maildir
    self.__name=maildir.mkbasename()
    self.__tmpname=os.path.join('tmp',self.__name)
    self.__newname=os.path.join('new',self.__name)
    self.__fp=open(maildir.fullpath(self.__tmpname),"w")

  def write(self,s):
    self.__fp.write(s)

  def close(self):
    self.__fp.close()
    oldname=self.__maildir.fullpath(self.__tmpname)
    newname=self.__maildir.fullpath(self.__newname)
    saferename(oldname,newname)
    return newname

_maildirs={}
def openMaildir(path):
  if path not in _maildirs:
    verbose("open new Maildir", path)
    _maildirs[path]=Maildir(path)
  return _maildirs[path]
