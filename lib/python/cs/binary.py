#!/usr/bin/env python3
#

''' Facilities associated with binary data.
    Requires Python 3 because a Python 2 bytes object is too weak,
    as is my cs.py.bytes hack class also.
    - Cameron Simpson <cs@cskk.id.au> 22jul2018
'''

from __future__ import print_function
from collections import defaultdict
from struct import Struct
import sys
from cs.buffer import CornuCopyBuffer

if sys.hexversion < 0x03000000:
  print(
      "WARNING: module %r requires Python 3, but sys.hexversion=0x%x"
      % (__name__, sys.hexversion),
      file=sys.stderr)

def flatten(chunks):
  ''' Flatten `chunks` into an iterable of bytes instances.
      This exists to allow subclass methods to easily return ASCII
      strings or bytes or iterables, in turn allowing them to
      simply return their superclass' chunks iterators directly
      instead of having to unpack them.
  '''
  if isinstance(chunks, bytes):
    yield chunks
  elif isinstance(chunks, str):
    yield chunks.encode('ascii')
  else:
    for subchunk in chunks:
      for chunk in flatten(subchunk):
        yield chunk

class PacketField(object):
  ''' A record for an individual packet field.
  '''

  def __init__(self, value):
    self.value = value

  def __str__(self):
    return "%s(%s)" % (type(self).__name__, self.value)

  @classmethod
  def from_bytes(cls, bs, offset=0, length=None):
    ''' Factory to return an PacketField instance from bytes.
        This relies on the class' from_bfr(CornuCopyBuffer) method.
    '''
    bfr = CornuCopyBuffer.from_bytes(bs, offset=offset, length=length)
    field = cls.from_buffer(bfr)
    post_offset = offset + bfr.offset
    return field, post_offset

def fixed_bytes_field(length, class_name=None):
  ''' Factory for PacketField subclasses built off fixed length byte strings.
  '''
  if length < 1:
    raise ValueError("length(%d) < 1" % (length,))
  class FixedBytesField(PacketField):
    ''' A field whose value is simply a fixed length bytes chunk.
    '''
    @classmethod
    def from_buffer(cls, bfr):
      ''' Obtain fixed bytes from the buffer.
      '''
      return cls(bfr.take(length))
    def transcribe(self):
      ''' Transcribe the fixed bytes.
      '''
      return self.value
  if class_name is None:
    class_name = FixedBytesField.__name__ + '_' + str(length)
  FixedBytesField.__name__ = class_name
  return FixedBytesField

class BytesesField(PacketField):
  ''' A field containing a list of bytes chunks.

      The following attributes are defined:
      .value        The gathered data as a list of bytes instances,
                    or None if the field was gathered with
                    `discard_data` true.
      .offset       The starting offset of the data.
      .end_offset   The ending offset of the data.
  '''

  def __str__(self):
    return "%s(%d:%d:%s)" % (
        type(self).__name__,
        self.offset,
        self.end_offset,
        "None" if self.value is None else "bytes[%d]" % len(self.value))

  @classmethod
  def from_buffer(cls, bfr, end_offset=None, discard_data=False, short_ok=False):
    ''' Gather from `bfr` until `end_offset`.

        `bfr`: the input buffer
        `end_offset`: the ending buffer offset; if this is Ellipsis
          then all the remaining data in `bfr` will be collection
        `discard_data`: discard the data, keeping only the offset information
        `short_ok`: if true, do not raise EOFError if there are
          insufficient data; the field's .end_offset value will be
          less than `end_offset`; the default is False
    '''
    if end_offset is None:
      raise ValueError("missing end_offset")
    offset0 = bfr.offset
    if end_offset is Ellipsis:
      # special case: gather up all the remaining data
      if discard_data:
        for _ in bfr:
          pass
        byteses = None
      else:
        byteses = list(bfr)
    else:
      # otherwise gather up a bounded range of bytes
      if end_offset < bfr.offset:
        raise ValueError("end_offset(%d) < bfr.offset(%d)" % (end_offset, bfr.offset))
      byteses = None if discard_data else []
      bfr.skipto(
          end_offset,
          copy_skip=( None if discard_data else byteses.append ),
          short_ok=short_ok)
    field = cls(byteses)
    field.offset = offset0
    field.end_offset = bfr.offset
    return field

  def transcribe(self):
    ''' Transcribe the bytes instances.
        Warning: this is raise an exception of the data have been discarded.
    '''
    for bs in self.value:
      yield bs

# A cache of 256 length runs of assorted bytes values as memoryviews
# as a mapping of bytes=>memoryview.
# In normal use these will be based on single byte bytes values.
_bytes_256s = defaultdict(lambda b: memoryview(b * 256))

class BytesRunField(PacketField):
  ''' A field containing a continuous run of a single bytes value.

      The following attributes are defined:
      * `length`: the length of the run
      * `bytes_value`: the repeated bytes value

      The property `value` is computed on the fly on every reference
      and returns a value obeying the buffer protocol: a bytes or
      memoryview object.
  '''

  def __init__(self, length, bytes_value):
    if length < 0:
      raise ValueError("invalid length(%r), should be >= 0" % (length,))
    if len(bytes_value) != 1:
      raise ValueError(
          "only single byte bytes_value is supported, received: %r"
          % (bytes_value,))
    self.length = length
    self.bytes_value = bytes_value

  @property
  def value(self):
    ''' The run of bytes, computed on the fly.

        Values where length <= 256 are cached.
    '''
    length = self.length
    if length == 0:
      return b''
    bytes_value = self.bytes_value
    if length == 1:
      return bytes_value
    if length <= 256:
      bs = _bytes_256s[bytes_value]
      if length < 256:
        bs = bs[:length]
      return bs
    return bytes_value * length

  @classmethod
  def from_buffer(cls, bfr, end_offset=None, bytes_value=b'\0'):
    ''' Parse a BytesRunField by just skipping the specified number of bytes.

        Note: this *does not* check that the skipped bytes contain `bytes_value`.

        Parameters:
        * `bfr`: the buffer to scan
        * `end_offset`: the end offset of the scan, which may be
          an int or Ellipsis to indicate a scan to the end of the
          buffer
        * `bytes_value`: the bytes value to replicate, default
          `b'\0'`; if this is an int then a single byte of that value
          is used
    '''
    if end_offset is None:
      raise ValueError("missing end_offset")
    if isinstance(bytes_value, int):
      bytes_value = bytes((bytes_value,))
    offset0 = bfr.offset
    if end_offset is Ellipsis:
      for _ in bfr:
        pass
    else:
      bfr.skipto(end_offset, discard_data=True)
    field = cls(bfr.offset - offset0, bytes_value)
    return field

  def transcribe(self):
    ''' Transcribe the BytesRunField in 256 byte chunks.
    '''
    length = self.length
    bytes_value = self.bytes_value
    bs256 = _bytes_256s[bytes_value]
    with length >= 256:
      yield bs256
      length -= 256
    if length > 0:
      yield bs256[:length]

def struct_field(format, class_name=None):
  ''' Factory for PacketField subclasses built around a single struct format.
  '''
  struct = Struct(format)
  class StructField(PacketField):
    ''' A PacketField subclass using a struct.Struct for parse and transcribe.
    '''
    @classmethod
    def from_buffer(cls, bfr):
      ''' Parse a value from the bytes `bs` at `offset`, default 0.
          Return a PacketField instance and the new offset.
      '''
      bs = bfr.take(struct.size)
      value, = struct.unpack(bs)
      return cls(value)
    def transcribe(self):
      ''' Transcribe the value back into bytes.
      '''
      return struct.pack(self.value)
  if class_name is not None:
    StructField.__name__ = class_name
  StructField.struct = struct
  StructField.format = format
  return StructField

# various common values

# an usigned 8 bit interger
UInt8 = struct_field('B')

# a big endian unsigned 32 bit integer
UInt32 = struct_field('>L')

# a big endian unsigned 64 bit integer
UInt64 = struct_field('>Q')

class Packet(PacketField):
  ''' Base class for compound objects derived from binary data.
  '''

  def __init__(self):
    # Packets are their own value
    PacketField.__init__(self, self)
    # start with no fields
    self.field_names = []
    self.fields = []
    self.field_map = {}

  def __str__(self):
    return "%s(%s)" % (
        type(self).__name__,
        ','.join(
            "%s=%s" % (field_name, self.field_map[field_name])
            for field_name in self.field_names
        )
    )

  def __getattr__(self, attr):
    ''' Unknown attributes may be field names; return their value.
    '''
    try:
      field = self.field_map[attr]
    except KeyError:
      raise AttributeError(attr)
    if field is None:
      return None
    return field.value

  def transcribe(self):
    ''' Yield a sequence of bytes objects for this instance.
    '''
    for field in self.fields:
      if field is not None:
        for bs in flatten(field.transcribe()):
          yield bs

  def add_from_bytes(self, field_name, bs, factory, offset=0, length=None, **kw):
    ''' Add a new PacketField named `field_name` parsed from the
        bytes `bs` using `factory`. Updates the internal field
        records.
        Returns the new PacketField's .value and the new parse
        offset within `bs`.

        `field_name`: the name for the new field; it must be new.
        `bs`: the bytes containing the field data; a CornuCopyBuffer
          is made from this for the parse.
        `factory`: a factory for parsing the field data, returning
          a PacketField. If `factory` is a class then its .from_buffer
          method is called, otherwise the factory is called directly.
        `offset`: optional start offset of the field data within
          `bs`, default 0.
        `length`: optional maximum number of bytes from `bs` to
          make available for the parse, default None meaning that
          everything from `offset` onwards is available.
        Additional keyword arguments are passed to the internal
        .add_from_buffer call.
    '''
    bfr = CornuCopyBuffer.from_bytes(bs, offset=offset, length=length)
    field = self.add_from_buffer(field_name, bfr, factory, **kw)
    return field, offset + bfr.offset

  def add_from_buffer(self, field_name, bfr, factory, **kw):
    ''' Add a new PacketField named `field_name` parsed from `bfr` using `factory`.
        Updates the internal field records.
        Returns the new PacketField's .value.

        `field_name`: the name for the new field; it must be new.
        `bfr`: a CornuCopyBuffer from which to parse the field data.
        `factory`: a factory for parsing the field data, returning
          a PacketField. If `factory` is a class then its .from_buffer
          method is called, otherwise the factory is called directly.
        Additional keyword arguments are passed to the internal
        factory call.
    '''
    from cs.x import X
    X("%s.add_from_buffer...", type(self).__name__)
    assert isinstance(field_name, str), "field_name not a str: %r" % (field_name,)
    assert isinstance(bfr, CornuCopyBuffer), "bfr not a CornuCopyBuffer: %r" % (bfr,)
    if isinstance(factory, type):
      from_buffer = factory.from_buffer
    else:
      from_buffer = factory
    field = from_buffer(bfr, **kw)
    self.add_field(field_name, field)
    return field.value

  def add_field(self, field_name, field):
    ''' Add a new PacketField `field` named `field_name`.
    '''
    if field_name in self.field_map:
      raise ValueError("field %r already in field_map" % (field_name,))
    self.field_names.append(field_name)
    self.fields.append(field)
    self.field_map[field_name] = field
    return None if field is None else field.value
