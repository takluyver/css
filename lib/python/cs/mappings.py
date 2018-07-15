#!/usr/bin/python -tt

''' Facilities for mappings and objects associated with mappings.
'''

from collections import defaultdict, namedtuple
from contextlib import contextmanager
from functools import partial
import re
from cs.sharedfile import SharedAppendLines
from cs.lex import isUC_, parseUC_sAttr
from cs.logutils import warning
from cs.py3 import StringTypes
from cs.seq import the

DISTINFO = {
    'description': "Facilities for mappings and objects associated with mappings.",
    'keywords': ["python2", "python3"],
    'classifiers': [
        "Programming Language :: Python",
        "Programming Language :: Python :: 2",
        "Programming Language :: Python :: 3",
    ],
    'install_requires': ['cs.sharedfile', 'cs.lex', 'cs.logutils', 'cs.py3', 'cs.seq' ],
}

class SeqMapUC_Attrs(object):
  ''' A wrapper for a mapping from keys (matching ^[A-Z][A-Z_0-9]*$)
      to tuples. Attributes matching such a key return the first element
      of the sequence (and requires the sequence to have exactly on element).
      An attribute FOOs or FOOes (ending in a literal 's' or 'es', a plural)
      returns the sequence (FOO must be a key of the mapping).
  '''
  def __init__(self, M, keepEmpty=False):
    self.__M = M
    self.keepEmpty = keepEmpty

  def __str__(self):
    kv = []
    for k, value in self.__M.items():
      if isUC_(k):
        if len(value) != 1:
          k = k + 's'
        else:
          value = value[0]
      kv.append((k, value))
    return '{%s}' % (", ".join([ "%s: %r" % (k, value) for k, value in kv ]))

  def __hasattr__(self, attr):
    k, _ = parseUC_sAttr(attr)
    if k is None:
      return k in self.__dict__
    return k in self.__M

  def __getattr__(self, attr):
    k, plural = parseUC_sAttr(attr)
    if k is None:
      return self.__dict__[k]
    if plural:
      if k not in self.__M:
        return ()
      return self.__M[k]
    return the(self.__M[k])

  def __setattr__(self, attr, value):
    k, plural = parseUC_sAttr(attr)
    if k is None:
      self.__dict__[attr] = value
      return
    if plural:
      if isinstance(type, StringTypes):
        raise ValueError("invalid string %r assigned to plural attribute %r" % (value, attr))
      T = tuple(value)
      if len(T) == 0 and not self.keepEmpty:
        if k in self.__M:
          del self.__M[k]
      else:
        self.__M[k] = T
    else:
      self.__M[k] = (value,)

  def __delattr__(self, attr):
    k, _ = parseUC_sAttr(attr)
    if k is None:
      del self.__dict__[k]
    else:
      del self.__M[k]

class UC_Sequence(list):
  ''' A tuple-of-nodes on which .ATTRs indirection can be done,
      yielding another tuple-of-nodes or tuple-of-values.
  '''
  def __init__(self, Ns):
    ''' Initialise from an iterable sequence.
    '''
    list.__init__(self, Ns)

  def __getattr__(self, attr):
    k, plural = parseUC_sAttr(attr)
    if k is None or not plural:
      return list.__getattr__(self, attr)
    values = tuple(self.__attrvals(attr))
    if len(values) > 0 and not isNode(values[0]):
      return values
    return _Nodes(values)

  def __attrvals(self, attr):
    for N in self.__nodes:
      for v in getattr(N, attr):
        yield v

class AttributableList(list):
  ''' An AttributableList maps unimplemented attributes onto the list members
      and returns you a new AttributableList with the results, ready for a
      further dereference.

      Example:

        >>> class C(object):
        ...   def __init__(self, i):
        ...     self.i = i
        >>> Cs = [ C(1), C(2), C(3) ]
        >>> AL = AttributableList( Cs )
        >>> print(AL.i)
        [1, 2, 3]
  '''

  def __init__(self, initlist=None, strict=False):
    ''' Initialise the list.
        The optional parameter `initlist` initialises the list
        as for a normal list.
        The optional parameter `strict`, if true, causes list elements
        lacking the attribute to raise an AttributeError. If false,
        list elements without the attribute are omitted from the results.
    '''
    if initlist:
      list.__init__(self, initlist)
    else:
      list.__init__(self)
    self.strict = strict

  def __getattr__(self, attr):
    if self.strict:
      result = [ getattr(item, attr) for item in self ]
    else:
      result = []
      for item in self:
        try:
          r = getattr(item, attr)
        except AttributeError:
          pass
        else:
          result.append(r)
    return AttributableList(result)

class MethodicalList(AttributableList):
  ''' A MethodicalList subclasses a list and maps unimplemented attributes
      into a callable which calls the corresponding method on each list members
      and returns you a new MethodicalList with the results, ready for a
      further dereference.

      Example:
        >>> n = 1
        >>> class C(object):
        ...   def __init__(self):
        ...     global n
        ...     self.n = n
        ...     n += 1
        ...   def x(self):
        ...     return self.n
        ...
        >>> Cs=[ C(), C(), C() ]
        >>> ML = MethodicalList( Cs )
        >>> print(ML.x())
        [1, 2, 3]
  '''

  def __init__(self, initlist=None, strict=False):
    ''' Initialise the list.
        The optional parameter `initlist` initialises the list
        as for a normal list.
        The optional parameter `strict`, if true, causes list elements
        lacking the attribute to raise an AttributeError. If false,
        list elements without the attribute are omitted from the results.
    '''
    AttributableList.__init__(self, initlist=initlist, strict=strict)

  def __getattr__(self, attr):
    return partial(self.__call_attr, attr)

  def __call_attr(self, attr):
    if self.strict:
      submethods = [ getattr(item, attr) for item in self ]
    else:
      submethods = []
      for item in self:
        try:
          submethod = getattr(item, attr)
        except AttributeError:
          pass
        else:
          submethods.append(submethod)
    return MethodicalList( method() for method in submethods )

class FallbackDict(defaultdict):
  ''' A dictlike object that inherits from another dictlike object;
      this is a convenience subclass of defaultdict.
  '''

  def __init__(self, otherdict):
    '''
    '''
    defaultdict.__init__(self)
    self.__otherdict = otherdict

  def __missing__(self, key):
    if key not in self:
      return self.__otherdict[key]
    raise KeyError(key)

class MappingChain(object):
  ''' A mapping interface to a sequence of mappings.
      It does not support __setitem__ at present; that is expected
      to be managed via the backing mappings.
  '''

  def __init__(self, mappings=None, get_mappings=None):
    ''' Initialise the MappingChain.
        `mappings`: initial sequence of mappings, default None.
        `get_mappings`: callable to obtain the initial sequence of
        Exactly one of `mappings` or `get_mappings` must be provided.
    '''
    if mappings is not None:
      if get_mappings is None:
        mappings = list(mappings)
        self.get_mappings = lambda: mappings
      else:
        raise ValueError(
            "cannot supply both mappings (%r) and get_mappings (%r)"
            % (mappings, get_mappings))
    else:
      if get_mappings is not None:
        self.get_mappings = get_mappings
      else:
        raise ValueError("one of mappings or get_mappings must be specified")

  def __getitem__(self, key):
    ''' Return the first value for `key` found in the mappings.
        Raise KeyError if the key in not found in any mapping.
    '''
    for mapping in self.get_mappings():
      try:
        value = mapping[key]
      except KeyError:
        continue
      return value
    raise KeyError(key)

  def get(self, key, default=None):
    ''' Get the value associated with `key`, return `default` if missing.
    '''
    try:
      return self[key]
    except KeyError:
      return default

  def __contains__(self, key):
    try:
      _ = self[key]
    except KeyError:
      return False
    return True

  def keys(self):
    ''' Return the union of the keys in the mappings.
    '''
    ks = set()
    for mapping in self.get_mappings():
      ks.update(mapping.keys())
    return ks

class SeenSet(object):
  ''' A set-like collection with optional backing store file.
  '''

  def __init__(self, name, backing_path=None):
    self.name = name
    self.backing_path = backing_path
    self.set = set()
    if backing_path is not None:
      # create file if missing, also tests access permission
      with open(backing_path, "a"):
        pass
      self._backing_file = SharedAppendLines(backing_path,
                                             importer=self._add_foreign_line)
      self._backing_file.ready()

  def _add_foreign_line(self, line):
    # EOF markers, discard
    if line is None:
      return
    if not line.endswith('\n'):
      warning("%s: adding unterminated line: %s", self, line)
    s = line.rstrip()
    self.add(s, foreign=True)

  def add(self, s, foreign=False):
    ''' Add the value `s` to the set.
        `s`: the value to add
        `foreign`: default False: whether the value came from an
          outside source, usually a third party addition to the backing
          file; this prevents appending the value to the backing file
    '''
    # avoid needlessly extending the backing file
    if s in self.set:
      return
    self.set.add(s)
    if not foreign and self.backing_path:
      self._backing_file.put(s)

  def __contains__(self, item):
    return item in self.set

class StackableValues(object):
  ''' A collection of named stackable values with the latest value
      available as an attribute.

      Note that names conflicting with methods are not available
      as attributes and must be accessed via __getitem__. As a
      matter of practice, in addition to the mapping methods, avoid
      names which are verbs or which begin with an underscore.

      >>> S = StackableValues()
      >>> print(S)
      StackableValues()
      >>> S.push('x', 1)
      >>> print(S)
      StackableValues(x=1)
      >>> print(S.x)
      1
      >>> S.push('x', 2)
      >>> print(S.x)
      2
      >>> S.x = 3
      >>> print(S.x)
      3
      >>> S.pop('x')
      3
      >>> print(S.x)
      1
      >>> with S.stack('x', 4):
      ...   print(S.x)
      ...
      4
      >>> print(S.x)
      1
  '''

  def __init__(self):
    self._values = defaultdict(list)

  def __str__(self):
    return (
        "%s(%s)"
        % (
            type(self).__name__,
            ','.join( "%s=%s" % (k, v) for k, v in sorted(self.items()) )
        )
    )

  def __repr__(self):
    return (
        "%s(%s)"
        % (
            type(self),
            ','.join( "%r=%r" % (k, v) for k, v in sorted(self.items()) )
        )
    )

  def keys(self):
    ''' Mapping method returning an iterable of the names.
    '''
    return self._values.keys()

  def values(self):
    ''' Mapping method returning an iterable of the values.
    '''
    for key in self.keys():
      try:
        v = self[key]
      except KeyError:
        pass
      else:
        yield v

  def items(self):
    ''' Mapping method returning an iterable of (name, value) tuples.
    '''
    for key in self.keys():
      try:
        v = self[key]
      except KeyError:
        pass
      else:
        yield key, v

  def __getattr__(self, attr):
    ''' Present the top value of key `attr` as an attribute.
    '''
    if attr.startswith('_'):
      raise AttributeError(attr)
    try:
      v = self[attr]
    except KeyError:
      raise AttributeError(attr)
    return v

  def __setattr__(self, attr, value):
    if attr.startswith('_'):
      self.__dict__[attr] = value
    else:
      try:
        vs = self._values[attr]
      except KeyError:
        raise AttributeError(attr)
      else:
        if not vs:
          raise AttributeError(attr)
        vs[-1] = value

  def __getitem__(self, key):
    ''' Return the top value for `key` or raise KeyError.
    '''
    vs = self._values[key]
    try:
      v = vs[-1]
    except IndexError:
      raise KeyError(key)
    return v

  def get(self, key, default=None):
    ''' Get the top value for `key`, or `default`.
    '''
    try:
      v = self[key]
    except KeyError:
      v = default
    return v

  def push(self, key, value):
    ''' Push a new `value` for `key`.
    '''
    self._values[key].append(value)

  def pop(self, key):
    ''' Pop and return the latest value for `key`.
    '''
    vs = self._values[key]
    try:
      v = vs.pop()
    except IndexError:
      raise KeyError(key)
    return v

  @contextmanager
  def stack(self, key, value):
    ''' Context manager which pushes and pops a new `value` for `key`.
    '''
    self.push(key, value)
    try:
      yield
    finally:
      self.pop(key)

def named_row_tuple(*column_names, **kw):
  ''' Return a namedtuple subclass factory derived from `column_names`.

      `column_names`: an iterable of str, such as the heading columns
        of a CSV export
      `class_name`: optional keyword parameter specifying the class name
      `computed`: optional keyword parameter providing a mapping
        of str to functions of `self`; these strings are available
        via __getitem__

      The tuple's attributes are computed by converting all runs
      of nonalphanumerics (as defined by the re module's "\W"
      sequence) to an underscore, lowercasing and then stripping
      leading and trailing underscores.

      In addition to the normal numeric indices, the tuple may
      also be indexed by the attribute names or the column names.

      The new class has the following additional attributes:
      `_attributes`: the attribute names of each tuple in order
      `_names`: the originating name strings
      `_name_attributes`: the computed attribute names corresponding to the
        `names`; there may be empty strings in this list
      `_attr_of`: a mapping of column name to attribute name
      `_name_of`: a mapping of attribute name to column name
      `_index_of`: a mapping of column names and attributes their tuple indices

      Examples::

        >>> T = named_row_tuple('Column 1', '', 'Column 3', ' Column 4', 'Column 5 ', '', '', class_name='Example')
        >>> T._attributes
        ['column_1', 'column_3', 'column_4', 'column_5']
        >>> row = T('val1', 'dropped', 'val3', 4, 5, 6, 7)
        >>> row
        Example(column_1='val1', column_3='val3', column_4=4, column_5=5)
  '''
  class_name = kw.pop('class_name', None)
  computed = kw.pop('computed', None)
  if kw:
    raise ValueError("unexpected keyword arguments: %r" % (kw,))
  if class_name is None:
    class_name = 'NamedRow'
  column_names = list(column_names)
  if computed is None:
    computed = {}
  # compute candidate tuple attributes from the column names
  name_attributes = [
      re.sub(r'\W+', '_', name).strip('_').lower()
      for name in column_names
  ]
  # final tuple attributes are the nonempty _name_attributes
  attributes = [ attr for attr in name_attributes if attr ]
  if len(attributes) == len(name_attributes):
    attributes = name_attributes

  _NamedRow = namedtuple(class_name, attributes)
  class NamedRow(_NamedRow):
    ''' A namedtuple to store row data.

        In addition to the normal numeric indices, the tuple may
        also be indexed by the attribute names or the column names.

        The class has the following attributes:
        `_attributes`: the attribute names of each tuple in order
        `_computed`: a mapping of str to functions of `self`; these
          values are also available via __getitem__
        `_names`: the originating name strings
        `_name_attributes`: the computed attribute names corresponding to the
          `names`; there may be empty strings in this list
        `_attr_of`: a mapping of column name to attribute name
        `_name_of`: a mapping of attribute name to column name
        `_index_of`: a mapping of column names and attributes their tuple indices
    '''

    _attributes = attributes
    _computed = computed
    _names = column_names
    _name_attributes = name_attributes
    _attr_of = {}   # map name to attr, omits those with empty/missing attrs
    _name_of = {}   # map attr to name
    _index_of = {}  # map name or attr to index
    i = 0
    for name, attr in zip(_names, _name_attributes):
      if attr:
        _attr_of[name] = attr
        _name_of[attr] = name
        _index_of[name] = i
    del i, name, attr
    _index_of.update( (s, i) for i, s in enumerate(_attributes) )

    def __getitem__(self, key):
      if isinstance(key, int):
        i = key
      elif isinstance(key, str):
        func = self._computed.get(key)
        if func is not None:
          return func(self)
        i = self._index_of[key]
      else:
        raise TypeError("expected int or str, got %s" % (type(key),))
      return _NamedRow.__getitem__(self, i)

  NamedRow.__name__ = class_name

  # make a factory to avoid tromping the namedtuple __new__/__init__
  def factory(*row):
    ''' Factory function to create a NamedRow from a raw row.
    '''
    if attributes is not name_attributes:
      row = [ item for item, attr in zip(row, name_attributes) if attr ]
    return NamedRow(*row)
  # pretty up the factory for external use
  factory.__name__ = 'factory(%s)' % (NamedRow.__name__,)
  factory._attributes = NamedRow._attributes
  factory._names = NamedRow._names
  factory._name_attributes = NamedRow._name_attributes
  factory._attr_of = NamedRow._attr_of
  factory._name_of = NamedRow._name_of
  factory._index_of = NamedRow._index_of
  return factory

def named_column_tuples(rows, class_name=None, column_names=None):
  ''' Process an iterable of data rows, with the first row being column names.
      `rows`: an iterable of rows, each an iterable of data values.
      `class_name`: option class name for the namedtuple class
      `column_names`: optional iterable of column names used as the basis for
        the namedtuple. If this is not provided then the first row from
        `rows` is taken to be the column names.
      Yields the generated namedtuple factory and then instances of the class
      for each row.

      Rows may be flat iterables in the same order as the column
      names or mappings keyed on the column names.

      If the column names contain empty strings they are dropped
      and the corresponding data row entries are also dropped. This
      is very common with spreadsheet exports with unused padding
      columns.

      Typical human readable column headings, also common in
      speadsheet exports, are lowercased and have runs of whitespace
      or punctuation turned into single underscores; trailing
      underscores then get dropped.

      Basic example::

        >>> data1 = [
        ...   ('a', 'b', 'c'),
        ...   (1, 11, "one"),
        ...   (2, 22, "two"),
        ... ]
        >>> rows = list(named_column_tuples(data1))
        >>> cls = rows.pop(0)
        >>> print(rows)
        [NamedRow(a=1, b=11, c='one'), NamedRow(a=2, b=22, c='two')]

      Human readable column headings::

        >>> data1 = [
        ...   ('Index', 'Value Found', 'Descriptive Text'),
        ...   (1, 11, "one"),
        ...   (2, 22, "two"),
        ... ]
        >>> rows = list(named_column_tuples(data1))
        >>> cls = rows.pop(0)
        >>> print(rows)
        [NamedRow(index=1, value_found=11, descriptive_text='one'), NamedRow(index=2, value_found=22, descriptive_text='two')]

      Rows which are mappings::

        >>> data1 = [
        ...   ('a', 'b', 'c'),
        ...   (1, 11, "one"),
        ...   {'a': 2, 'c': "two", 'b': 22},
        ... ]
        >>> rows = list(named_column_tuples(data1))
        >>> cls = rows.pop(0)
        >>> print(rows)
        [NamedRow(a=1, b=11, c='one'), NamedRow(a=2, b=22, c='two')]

      CSV export with unused padding columns::

        >>> data1 = [
        ...   ('a', 'b', 'c', '', ''),
        ...   (1, 11, "one"),
        ...   {'a': 2, 'c': "two", 'b': 22},
        ...   [3, 11, "three", '', 'dropped'],
        ... ]
        >>> rows = list(named_column_tuples(data1, 'CSV_Row'))
        >>> cls = rows.pop(0)
        >>> print(rows)
        [CSV_Row(a=1, b=11, c='one'), CSV_Row(a=2, b=22, c='two'), CSV_Row(a=3, b=11, c='three')]

  '''
  if column_names is None:
    cls = None
  else:
    cls = named_row_tuple(*column_names, class_name=class_name)
    yield cls
    tuple_attributes = cls._attributes
    name_attributes = cls._name_attributes
  for row in rows:
    if cls is None:
      column_names = row
      cls = named_row_tuple(*column_names, class_name=class_name)
      yield cls
      tuple_attributes = cls._attributes
      name_attributes = cls._name_attributes
      continue
    if callable(getattr(row, 'get', None)):
      # flatten a mapping into a list ordered by column_names
      row = [ row.get(k) for k in column_names ]
    if tuple_attributes is not name_attributes:
      # drop items from columns with empty names
      row = [ item for item, attr in zip(row, name_attributes) if attr ]
    yield cls(*row)
