from __future__ import print_function
import os
import os.path
import errno
import sys
import logging
info = logging.info
warning = logging.warning
import string
import time
if sys.hexversion < 0x02060000: from sets import Set as set
from cs.lex import parseline, strlist

T_SEQ = 'ARRAY'
T_MAP = 'HASH'
T_SCALAR = 'SCALAR'
def objFlavour(obj):
  """ Return the ``flavour'' of an object:
      T_MAP: DictType, DictionaryType, objects with an __keys__ or keys attribute.
      T_SEQ: TupleType, ListType, objects with an __iter__ attribute.
      T_SCALAR: Anything else.
  """
  t = type(obj)
  if isinstance(t, (tuple, list)):
    return T_SEQ
  if isinstance(t, dict):
    return T_MAP
  if hasattr(obj, '__keys__') or hasattr(obj, 'keys'):
    return T_MAP
  if hasattr(obj, '__iter__'):
    return T_SEQ
  return T_SCALAR

class WithUCAttrs:
  ''' An object where access to obj.FOO accesses obj['FOO']
      if FOO is all upper case.
  '''
  def __getattr__(self, attr):
    if attr.isalpha() and attr.isupper():
      return self[attr]
    return dict.__getattr__(self, attr)
  def __setattr__(self, attr, value):
    if attr.isalpha() and attr.isupper():
      self[attr]=value
      return
    self.__dict__[attr]=value

class DictUCAttrs(dict, WithUCAttrs):
  ''' A dict where access to obj.FOO accesses obj['FOO']
      if FOO is all upper case.
  '''
  def __init__(self, fill=None):
    if fill is None:
      fill=()
    dict.__init__(self, fill)

class WithUC_Attrs:
  ''' An object where access to obj.FOO accesses obj['FOO']
      if FOO matches ^[A-Z][_A-Z0-9]*.
  '''
  def __uc_(self, s):
    if s.isalpha() and s.isupper():
      return True
    if len(s) < 1:
      return False
    if not s[0].isupper():
      return False
    for c in s[1:]:
      if c != '_' and not (c.isupper() or c.isdigit()):
        return False
    return True
  def __getattr__(self, attr):
    if self.__uc_(attr):
      return self[attr]
    return dict.__getattr__(self, attr)
  def __setattr__(self, attr, value):
    if self.__uc_(attr):
      self[attr]=value
      return
    self.__dict__[attr]=value

class DictUC_Attrs(dict, WithUC_Attrs):
  ''' A dict where access to obj.FOO accesses obj['FOO']
      if FOO matches ^[A-Z][_A-Z0-9]*.
  '''
  def __init__(self, fill=None):
    if fill is None:
      fill=()
    dict.__init__(self, fill)

class DictAttrs(dict):
  def __init__(self, d=None):
    dict.__init__()
    if d is not None:
      for k in d.keys():
        self[k]=d[k]

  def __getattr__(self, attr):
    return self[attr]
  def __setattr__(self, attr, value):
    self[attr]=value

def O_str(o, no_recurse=False):
  omit = getattr(o, '_O_omit', ())
  return ( "<%s %s>"
           % ( o.__class__.__name__,
               (    str(o)
                 if type(o) in (tuple,)
                 else
                       "<%s len=%d>" % (type(o), len(o))
                    if type(o) in (set,)
                    else
                       ",".join([ ( "%s=<%s>" % (pattr, type(pvalue).__name__)
                                    if no_recurse else
                                    "%s=%s" % (pattr, pvalue)
                                  )
                                  for pattr, pvalue
                                  in [ (attr, getattr(o, attr))
                                       for attr in sorted(dir(o))
                                       if attr[0].isalpha()
                                          and not attr in omit
                                     ]
                                  if not callable(pvalue)
                                ])
               )
             )
         )

class O(object):
  ''' A bare object subclass to allow storing arbitrary attributes.
      It also has a nicer default str() action.
  '''

  _O_recurse = True

  def __init__(self, **kw):
    ''' Initialise this O.
        Fill in attributes from any keyword arguments if supplied.
        This call can be omitted in subclasses if desired.
    '''
    for k in kw:
      setattr(self, k, kw[k])

  def __str__(self):
    recurse = self._O_recurse
    self._O_recurse = False
    s = O_str(self, no_recurse = not recurse)
    self._O_recurse = recurse
    return s

def unimplemented(func):
  ''' Decorator for stub methods that must be implemented by a stub class.
  '''
  def wrapper(self, *a, **kw):
    raise NotImplementedError("%s.%s(*%s, **%s)" % (type(self), func.__name__, a, kw))
  return wrapper

class slist(list):
  ''' A list with a shorter str().
  '''

  def __str__(self):
    return "[" + ",".join(str(e) for e in self) + "]"
