#!/usr/bin/python
#
# MacOSX plist facilities. Supports binary plist files, which the
# stdlib plistlib module does not.
#       - Cameron Simpson <cs@zip.com.au>
#

import base64
import os
import plistlib
import shutil
import subprocess
import tempfile
import cs.iso8601
import cs.sh
from cs.xml import etree
from .iphone import is_iphone
from cs.logutils import X

def import_as_etree(plist):
  ''' Load an Apple plist and return an etree.Element.
      `plist`: the source plist: data if bytes, filename if str,
          otherwise a file object open for binary read.
  '''
  if isinstance(plist, bytes):
    # read bytes as a data stream
    # write to temp file, decode using plutil
    with tempfile.NamedTemporaryFile() as tfp:
      tfp.write(plist)
      tfp.flush()
      tfp.seek(0, 0)
      return import_as_etree(tfp.file)
  if isinstance(plist, str):
    # presume plist is a filename
    with open(plist, "rb") as pfp:
      return import_as_etree(pfp)
  # presume plist is a file
  P = subprocess.Popen(['plutil', '-convert', 'xml1', '-o', '-', '-'],
                       stdin=plist,
                       stdout=subprocess.PIPE)
  E = etree.parse(P.stdout)
  retcode = P.wait()
  if retcode != 0:
    raise ValueError("export_xml_as_plist(E=%s,...): plutil exited with returncode=%s" % (E, retcode))
  return E

def ingest_plist(plist):
  ''' Ingest an Apple plist and return as a PListDict.
      Trivial wrapper for import_as_etree and ingest_plist_etree.
  '''
  return ingest_plist_etree(import_as_etree(plist))

def export_xml_to_plist(E, fp=None, fmt='binary1'):
  ''' Export the content of an etree.Element to a plist file.
      `E`: the source etree.Element.
      `fp`: the output file or filename (if a str).
      `fmt`: the output format, default "binary1". The format must
              be a valid value for the "-convert" option of plutil(1).
  '''
  if isinstance(fp, str):
    with open(fp, "wb") as ofp:
      return export_xml_as_plist(E, ofp, fmt=fmt)
  P = subprocess.Popen(['plutil', '-convert', fmt, '-o', '-', '-'],
                       stdin=subprocess.PIPE,
                       stdout=fp)
  P.stdin.write(etree.tostring(E))
  P.stdin.close()
  retcode = P.wait()
  if retcode != 0:
    raise ValueError("export_xml_as_plist(E=%s,...): plutil exited with returncode=%s" % (E, retcode))

def ingest_plist_etree(plist_etree):
  ''' Recursively a plist's ElementTree into a native Python structure.
      This returns a PListDict, a mapping of the plists's top dict
      with attribute access to key values.
  '''
  root = plist_etree.getroot()
  if root.tag != 'plist':
    raise ValueError("%r root Element is not a plist: %r" % (plist_root, root))
  return ingest_plist_dict(root[0])

def ingest_plist_elem(e):
  ''' Ingest a plist Element, converting various types to native Python objects.
      Unhandled types remain as the original Element.
  '''
  if e.tag == 'dict':
    return ingest_plist_dict(e)
  if e.tag == 'array':
    return ingest_plist_array(e)
  if e.tag == 'string' or e.tag == 'key':
    return e.text
  if e.tag == 'integer':
    return int(e.text)
  if e.tag == 'real':
    return float(e.text)
  if e.tag == 'false':
    return False
  if e.tag == 'true':
    return True
  if e.tag == 'data':
    return base64.b64decode(e.text)
  if e.tag == 'date':
    return cs.iso8601.parseZ(e.text)
  X("NOT TRANSFORMING plist elem %r: %r %r", e, e.attrib, e.text)
  return e

def ingest_plist_array(pa):
  ''' Ingest a plist <array>, returning a Python list.
  '''
  if pa.tag != 'array':
    raise ValueError("not an <array>: %r" % (pa,))
  a = []
  for i in range(len(pa)):
    e = pa[i]
    a.append(ingest_plist_elem(e))
  return a

def ingest_plist_dict(pd):
  ''' Ingest a plist <dict> Element, returning a PListDict.
  '''
  if pd.tag != 'dict':
    raise ValueError("not a <dict>: %r" % (pd,))
  d = PListDict()
  for i in range(len(pd)):
    e = pd[i]
    if i%2 == 0:
      if e.tag == 'key':
        key = e.text
      else:
        raise ValueError("unexpected key element %r" % (e,))
    else:
      value = ingest_plist_elem(e)
      d[key] = value
      key = None
  if key is not None:
    raise ValueError("no value for key %r" % (key,))
  return d

class PListDict(dict):
  ''' A mapping for a plist, subclassing dict, which also allows access to the elements by attribute if that does not conflict with a dict method.
  '''
  def __getattr__(self, attr):
    if attr[0].isalpha():
      try:
        return self[attr]
      except KeyError:
        raise AttributeError(attr)
    raise AttributeError(attr)
  def __setattr__(self, attr, value):
    if attr in self:
      self[attr] = value
    else:
      super().__setattr__(attr, value)

####################################################################################
# Old routines written for use inside my jailbroken iPhone.
#

def readPlist(path, binary=False):
  if not binary:
    return plistlib.readPlist(path)
  tfd, tpath = tempfile.mkstemp()
  os.close(tfd)
  if is_iphone():
    shutil.copyfile(path,tpath)
    plargv=('plutil',
            '-c',
            'xml1',
            tpath)
  else:
    plargv=('plutil',
            '-convert',
            'xml1',
            '-o',
            tpath,
            path)
  os.system("set -x; exec "+" ".join(cs.sh.quote(plargv)))
  pl = plistlib.readPlist(tpath)
  os.unlink(tpath)
  return pl

def writePlist(rootObj, path, binary=False):
  if not binary:
    return plistlib.writePlist(rootObj, path)
  tfd, tpath = tempfile.mkstemp()
  os.close(tfd)
  plistlib.writePlist(rootObj, tpath)
  if is_iphone():
    shutil.copyfile(path,tpath)
    plargv=('plutil',
            '-c',
            'binary1',
            tpath)
  else:
    plargv=('plutil',
            '-convert',
            'binary1',
            '-o',
            path,
            tpath)
  os.system("set -x; exec "+" ".join(cs.sh.quote(plargv)))
  if is_iphone():
    shutil.copyfile(tpath,path)
