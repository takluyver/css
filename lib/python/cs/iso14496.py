#!/usr/bin/python
#

'''
Facilities for ISO14496 files - the ISO Base Media File Format,
the basis for several things including MP4.
- Cameron Simpson <cs@cskk.id.au> 26mar2016

ISO make the standard available here:
* [link](http://standards.iso.org/ittf/PubliclyAvailableStandards/index.html)
* [link](http://standards.iso.org/ittf/PubliclyAvailableStandards/c068960_ISO_IEC_14496-12_2015.zip)
'''

from __future__ import print_function
from collections import namedtuple, defaultdict
from functools import partial
import os
from os.path import basename
from struct import Struct
import sys
from cs.binary import (
    flatten as flatten_chunks,
    Packet, PacketField, BytesField, BytesesField, ListField,
    UInt8, Int16BE, Int32BE, UInt16BE, UInt32BE, UInt64BE, UTF8NULField,
    fixed_bytes_field, multi_struct_field, structtuple,
)
from cs.buffer import CornuCopyBuffer
from cs.excutils import logexc
from cs.logutils import setup_logging, warning, error
from cs.pfx import Pfx, XP
from cs.py.func import prop
from cs.py3 import bytes, pack, unpack, iter_unpack

DISTINFO = {
    'keywords': ["python3"],
    'classifiers': [
        "Development Status :: 4 - Beta",
        "Environment :: Console",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
    ],
    'install_requires': [],
}

USAGE = '''Usage:
  %s extract [-H] filename boxref output
            Extract the referenced Box from the specified filename into output.
            -H  Skip the Box header.
  %s [parse [{-|filename}]...]
            Parse the named files (or stdin for "-").
  %s test   Run unit tests.'''

def main(argv):
  cmd = basename(argv.pop(0))
  usage = USAGE % (cmd, cmd, cmd)
  setup_logging(cmd)
  if not argv:
    argv = ['parse']
  badopts = False
  op = argv.pop(0)
  with Pfx(op):
    if op == 'parse':
      if not argv:
        argv = ['-']
      for spec in argv:
        with Pfx(spec):
          if spec == '-':
            parsee = sys.stdin.fileno()
          else:
            parsee = spec
          nboxes = 0
          for B in parse(parsee, discard_data=True):
            nboxes += 1
            B.dump()
    elif op == 'extract':
      skip_header = False
      if argv and argv[0] == '-H':
        argv.pop(0)
        skip_header = True
      if not argv:
        warning("missing filename")
        badopts = True
      else:
        filename = argv.pop(0)
      if not argv:
        warning("missing boxref")
        badopts = True
      else:
        boxref = argv.pop(0)
      if not argv:
        warning("missing output")
        badopts = True
      else:
        output = argv.pop(0)
      if argv:
        warning("extra argments after boxref: %s", ' '.join(argv))
        badopts = True
      if not badopts:
        BX = Boxes()
        BX.load(filename)
        for B in BX:
          B.dump()
        B = BX[boxref][0]
        with Pfx(filename):
          fd = os.open(filename, os.O_RDONLY)
          fdout = sys.stdout.fileno()
          bfr = CornuCopyBuffer.from_fd(fd)
          offset = B.offset
          need = B.length
          if skip_header:
            offset += B.header_length
            if need is not None:
              need -= B.header_length
          bfr.seek(offset)
          with Pfx(output):
            with open(output, 'wb') as ofp:
              for chunk in bfr:
                if need is not None and need < len(chunk):
                  chunk = chunk[need]
                ofp.write(chunk)
                need -= len(chunk)
          os.close(fd)
    elif op == 'test':
      import cs.iso14496_tests
      cs.iso14496_tests.selftest(["%s: %s" % (cmd, op)] + argv)
    else:
      warning("unknown op")
      badopts = True
  if badopts:
    print(usage, file=sys.stderr)
    return 2
  return 0

class Boxes(list):

  def __init__(self):
    self.boxes = []
    self.by_type = defaultdict(list)
    self.by_path = defaultdict(list)

  def append(self, box):
    ''' Store a Box, indexing it by sequence and box_type_s and box_type_path.
    '''
    self.boxes.append(box)
    self.by_type[box.box_type_s].append(box)
    self.by_path[box.box_type_path].append(box)

  def __getitem__(self, key):
    if isinstance(key, str):
      if '.' in key:
        mapping = self.by_path
      else:
        mapping = self.by_type
      if key not in mapping:
        raise KeyError(key)
      return mapping[key]
    return self.boxes[key]

  def __iter__(self):
    return iter(self.boxes)

  def load(self, o):
    ''' Load the boxes from `o`.
    '''
    def copy_boxes(box):
      self.append(box)
    for box in parse(o, discard_data=True, copy_boxes=copy_boxes):
      pass

# a convenience chunk of 256 zero bytes, mostly for use by 'free' blocks
B0_256 = bytes(256)

# an arbitrary maximum read size for fetching the data section
SIZE_16MB = 1024*1024*16

class BoxHeader(Packet):
  ''' An ISO14496 Box header packet.
  '''

  @classmethod
  def from_buffer(cls, bfr):
    ''' Decode a box header from the CornuCopyBuffer `bfr`.
    '''
    header = cls()
    # note start of header
    header.offset = bfr.offset
    box_size = header.add_from_buffer('box_size', bfr, UInt32BE)
    box_type = header.add_from_buffer('box_type', bfr, 4)
    if box_size == 0:
      # box extends to end of data/file
      header.length = Ellipsis
    elif box_size == 1:
      # 64 bit length
      length = header.add_from_buffer('length', bfr, UInt64BE)
    else:
      # other box_size values are the length
      header.length = box_size
    if box_type == b'uuid':
      # user supplied 16 byte type
      user_type = header.add_from_buffer('user_type', bfr, 16)
    else:
      header.user_type = None
    # note end of header
    header.end_offset = bfr.offset
    header.type = box_type
    return header

class Box(Packet):
  ''' Base class for all boxes - ISO14496 section 4.2.

      This has the following PacketFields:
      * `header`: a BoxHeader
      * `body`: a BoxBody instance, usually a specific subclass
      * `unparsed`: if there are unconsumed bytes from the Box they
        are stored as here as a BytesesField; note that this field
        is not present if there were no unparsed bytes
  '''

  def __init__(self, parent=None):
    super().__init__()
    self.parent = parent

  def __str__(self):
    type_name = self.box_type_path
    try:
      body = self.body
    except AttributeError:
      return "%s:NO_BODY" % (type_name,)
    else:
      return "%s:%s" % (type_name, body)

  def self_check(self):
    ''' Sanity check this Box.
    '''
    # sanity check the supplied box_type
    # against the box types this class supports
    box_type = self.header.type
    try:
      BOX_TYPE = self.BOX_TYPE
    except AttributeError:
      try:
        BOX_TYPES = self.BOX_TYPES
      except AttributeError:
        if type(self) is not Box:
          raise RuntimeError(
              "no box_type check in %s, box_type=%r"
              % (self.__class__, box_type))
        pass
      else:
        if box_type not in BOX_TYPES:
          warning(
              "box_type should be in %r but got %r",
              BOX_TYPES, bytes(box_type))
    else:
      if box_type != BOX_TYPE:
        warning("box_type should be %r but got %r", BOX_TYPE, box_type)

  @classmethod
  def from_buffer(cls, bfr, discard_data=False, default_type=None, copy_boxes=None):
    ''' Decode a Box from `bfr`.

        Parameters:
        * `bfr`: the input CornuCopyBuffer
        * `discard_data`: if false (default), keep the unparsed data portion as
          a list of data chunks in the field .unparsed; if true, discard the
          unparsed data
        * `default_type`: default Box body type if no class is
          registered for the header box type.
        * `copy_boxes`: optional callable for reporting new Box instances

        This provides the Packet.from_buffer method, but offloads
        the actual parse to the method `parse_buffer`, which is
        overridden by subclasses.
    '''
    B = cls()
    B.offset = bfr.offset
    with Pfx("Box %s: parse_buffer", type(B).__name__):
      try:
        B.parse_buffer(bfr, discard_data=discard_data)
      except EOFError as e:
        error("EOF parsing buffer: %s", e)
    B.end_offset = bfr.offset
    if copy_boxes:
      copy_boxes(B)
    return B

  def parse_buffer(self, bfr, discard_data=False, default_type=None, copy_boxes=None):
    ''' Parse the Box from `bfr`.

        Parameters:
        * `bfr`: the input CornuCopyBuffer
        * `discard_data`: if false (default), keep the unparsed data portion as
          a list of data chunks in the field .unparsed; if true, discard the
          unparsed data
        * `default_type`: default Box body type if no class is
          registered for the header box type.
        * `copy_boxes`: optional callable for reporting new Box instances

        This method should be overridden by subclasses (if any,
        since the actual subclassing happens with the BoxBody base
        class).
    '''
    header = self.add_from_buffer('header', bfr, BoxHeader)
    bfr.report_offset(self.offset)
    length = header.length
    if length is Ellipsis:
      end_offset = Ellipsis
      bfr_tail= bfr
      warning("Box.parse_buffer: Box %s has no length", header)
    else:
      end_offset = self.offset + length
      bfr_tail = bfr.bounded(end_offset)
    body_class = pick_box_class(header.type, default_type=default_type)
    with Pfx("parse(%s:%s)", body_class.__name__, self.box_type_s):
      self.add_from_buffer(
          'body', bfr_tail, body_class, box=self,
          discard_data=discard_data, copy_boxes=copy_boxes)
      # advance over the remaining data, optionally keeping it
      self.unparsed_offset = bfr_tail.offset
      if (
          bfr_tail.at_eof()
          if end_offset is Ellipsis
          else end_offset > bfr_tail.offset
      ):
        # there are unparsed data, stash it away and emit a warning
        unparsed = self.add_from_buffer(
            'unparsed', bfr_tail, BytesesField,
            end_offset=end_offset, discard_data=discard_data)
        warning(
            "%s:%s: unparsed data: %d bytes",
            type(self).__name__, self.box_type_s, len(self['unparsed']))
      if bfr_tail is not bfr:
        bfr_tail.flush()

  @property
  def length(self):
    ''' The Box length, computed as `self.end_offset - self.offset`.
    '''
    return self.end_offset - self.offset

  @property
  def box_type(self):
    ''' The Box header type.
    '''
    return self.header.type

  @property
  def box_type_s(self):
    ''' The Box header type as a string.

        If the header type bytes decode as ASCII, return that,
        otherwise the header bytes' repr().
    '''
    box_type_b = bytes(self.box_type)
    try:
      box_type_name = box_type_b.decode('ascii')
    except UnicodeDecodeError:
      box_type_name = repr(box_type_b)
    return box_type_name

  @property
  def box_type_path(self):
    ''' The type path to this Box.
    '''
    types = [self.box_type_s]
    box = self.parent
    while box is not None:
      try:
        path_elem = box.box_type_s
      except AttributeError as e:
        raise RuntimeError(
            "%s.box_type_path: no .box_type_s on %r: %s"
            % (type(self).__name__, box, e))
      types.append(path_elem)
      box = box.parent
    return '.'.join(reversed(types))

  @property
  def user_type(self):
    return self.header.user_type

  # NB: a @property instead of @prop to preserve AttributeError
  @property
  def BOX_TYPE(self):
    ''' The default .BOX_TYPE is inferred from the class name.
    '''
    return type(self).boxbody_type_from_klass()

  def attribute_summary(self):
    ''' Comma separator list of attribute values honouring format strings.
    '''
    strs = []
    for attr in self.ATTRIBUTES:
      # use str(self.attr)
      if isinstance(attr, str):
        value = getattr(self, attr)
        s = str(value)
      else:
        # an (attr, fmt) tuple
        attr, fmt = attr
        value = getattr(self, attr)
        if isinstance(fmt, str):
          s = fmt % (value,)
        else:
          # should be a callable
          s = fmt(value)
      strs.append(attr + '=' + s)
    return ','.join(strs)

  def dump(self, indent='', fp=None, crop_length=170):
    if fp is None:
      fp = sys.stdout
    fp.write(indent)
    summary = str(self)
    if len(summary) > crop_length - len(indent):
      summary = summary[:crop_length - len(indent) - 4] + '...)'
    fp.write(summary)
    fp.write('\n')
    try:
      body = self.body
    except AttributeError:
      fp.write(indent)
      fp.write("NO BODY?")
      fp.write('\n')
    else:
      for field_name in body.field_names:
        field = body[field_name]
        if isinstance(field, SubBoxesField):
          fp.write(indent)
          fp.write('  ')
          fp.write(field_name)
          fp.write(':\n')
          for subbox in field.value:
            subbox.dump(indent=indent + '    ', fp=fp, crop_length=crop_length)

# mapping of known box subclasses for use by factories
KNOWN_BOXBODY_CLASSES = {}

def add_body_class(klass):
  ''' Register a box body class in KNOWN_BOXBODY_CLASSES.
  '''
  global KNOWN_BOXBODY_CLASSES
  with Pfx("add_body_class(%s)", klass):
    try:
      box_types = klass.BOX_TYPES
    except AttributeError:
      box_type = klass.boxbody_type_from_klass()
      box_types = (box_type,)
    for box_type in box_types:
      if box_type in KNOWN_BOXBODY_CLASSES:
        raise TypeError("box_type %r already in KNOWN_BOXBODY_CLASSES as %s"
                        % (box_type, KNOWN_BOXBODY_CLASSES[box_type]))
      KNOWN_BOXBODY_CLASSES[box_type] = klass

def add_body_subclass(superclass, box_type, section, desc):
  ''' Create and register a new Box class that is simply a subclass of another.
      Returns the new class.
  '''
  if isinstance(box_type, bytes):
    classname = box_type.decode('ascii').upper() + 'BoxBody'
  else:
    classname = box_type.upper() + 'BoxBody'
    box_type = box_type.encode('ascii')
  K = type(classname, (superclass,), {})
  K.__doc__ = (
      "Box type %r %s box - ISO14496 section %s."
      % (box_type, desc, section)
  )
  add_body_class(K)
  return K

def pick_box_class(box_type, default_type=None):
  ''' Infer the Python Box subclass from the bytes `box_type`.

      * `box_type`: the 4 byte box type
      * `default_type`: the default Box subclass, default None; if
        None, use Box.
  '''
  global KNOWN_BOXBODY_CLASSES
  if default_type is None:
    default_type = BoxBody
  return KNOWN_BOXBODY_CLASSES.get(box_type, default_type)

class SubBoxesField(ListField):
  ''' A field which is itself a list of Boxes.
  '''

  @classmethod
  def from_buffer(
      cls,
      bfr,
      end_offset=None, max_boxes=None,
      default_type=None,
      copy_boxes=None,
      parent=None):
    ''' Read Boxes from `bfr`, return a new SubBoxesField instance.
    '''
    if end_offset is None:
      raise ValueError("missing end_offset")
    boxes = []
    boxes_field = cls(boxes)
    while (
        (max_boxes is None or len(boxes) < max_boxes)
        and (end_offset is Ellipsis or bfr.offset < end_offset)
        and not bfr.at_eof()
    ):
      B = Box.from_buffer(bfr, default_type=default_type, copy_boxes=copy_boxes)
      B.parent = parent
      boxes.append(B)
    if end_offset is not Ellipsis and bfr.offset > end_offset:
      raise ValueError(
          "contained Boxes overran end_offset:%d by %d bytes"
          % (end_offset, bfr.offset - end_offset))
    return boxes_field

class BoxBody(Packet):
  ''' Abstract basis for all Box bodies.
  '''

  @classmethod
  def from_buffer(cls, bfr, box=None, **kw):
    ''' Create a BoxBody and fill it in via its `parse_buffer` method.

        Note that this function is expected to be called from
        `Box.from_buffer` and therefore that `bfr` is expected to
        be a bounded CornuCopyBuffer if the Box length is specified.
        Various BoxBodies gather some data "until the end of the
        Box", and we rely on this bound rather than keeping a close
        eye on some unsupplied "end offset" value.
    '''
    B = cls()
    B.box = box
    B.parse_buffer(bfr, **kw)
    return B

  def parse_buffer(self, bfr, discard_data=False, copy_boxes=None):
    ''' Gather the Box body fields from `bfr`.

        A generic BoxBody has no additional fields. Subclasses call
        their superclass' `parse_buffer` and then gather their
        specific fields.
    '''
    pass

  @classmethod
  def boxbody_type_from_klass(klass):
    ''' Compute the Box's 4 byte type field from the class name.
    '''
    klass_name = klass.__name__
    if len(klass_name) == 11 and klass_name.endswith('BoxBody'):
      klass_prefix = klass_name[:4]
      if klass_prefix.rstrip('_').isupper():
        return klass_prefix.replace('_', ' ').lower().encode('ascii')
    raise AttributeError("no automatic box type for %s" % (klass,))

class FullBoxBody(BoxBody):
  ''' A common extension of a basic BoxBody, with a version and flags field.
      ISO14496 section 4.2.
  '''

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, **kw)
    self.add_field('version', UInt8.from_buffer(bfr))
    self.add_field('flags0', UInt8.from_buffer(bfr))
    self.add_field('flags1', UInt8.from_buffer(bfr))
    self.add_field('flags2', UInt8.from_buffer(bfr))

  @property
  def flags(self):
    return (self.flags0<<16) | (self.flags1<<8) | self.flags2

add_body_subclass(BoxBody, 'mdat', '8.1.1.1', 'Media Data')

class FREEBoxBody(BoxBody):
  ''' A 'free' or 'skip' box - ISO14496 section 8.1.2.
      Note the length and discard the data portion.
  '''

  BOX_TYPES = (b'free', b'skip')

  def parse_buffer(self, bfr, end_offset=None, **kw):
    super().parse_buffer(bfr, **kw)
    offset0 = bfr.offset
    self.add_from_buffer('padding', BytesRunField, end_offset=end_offset)
    self.free_size = bfr.offset - offset0

add_body_class(FREEBoxBody)

class FTYPBoxBody(BoxBody):
  ''' An 'ftyp' File Type box - ISO14496 section 4.3.
      Decode the major_brand, minor_version and compatible_brands.
  '''

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, **kw)
    self.add_from_buffer('major_brand', bfr, 4)
    self.add_from_buffer('minor_version', bfr, UInt32BE)
    brands_bs = b''.join(bfr)
    self.add_field('brands_bs', fixed_bytes_field(len(brands_bs))(brands_bs))

  @property
  def compatible_brands(self):
    return [
        self.brands_bs[offset:offset+4]
        for offset in range(0, len(self.brands_bs), 4)
     ]

add_body_class(FTYPBoxBody)

# field names for the tuples in a PDINBoxBody
PDInfo = structtuple('PDInfo', '>LL', 'rate initial_delay')

class PDINBoxBody(FullBoxBody):
  ''' An 'pdin' Progressive Download Information box - ISO14496 section 8.1.3.
      Decode the (rate, initial_delay) pairs of the data section.
  '''

  ATTRIBUTES = (('pdinfo', '%r'),)

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, **kw)
    # obtain box data after version and flags decode
    pdinfo = []
    while not bfr.at_eof():
      pdinfo.append(PDInfo.from_buffer(bfr))
    self.add_field('pdinfo', ListField(pdinfo))

add_body_class(PDINBoxBody)

class ContainerBoxBody(BoxBody):
  ''' A base class for pure container boxes.
  '''

  def parse_buffer(self, bfr, default_type=None, copy_boxes=None, **kw):
    super().parse_buffer(bfr, copy_boxes=copy_boxes, **kw)
    boxes = self.add_from_buffer('boxes', bfr, SubBoxesField, end_offset=Ellipsis, parent=self.box)

  def dump(self, indent='', fp=None):
    if fp is None:
      fp = sys.stdout
    fp.write(indent)
    fp.write(self.__class__.__name__)
    fp.write('\n')
    indent += '  '
    for B in self.boxes:
      B.dump(indent, fp)

class MOOVBoxBody(ContainerBoxBody):
  ''' An 'moov' Movie box - ISO14496 section 8.2.1.
      Decode the contained boxes.
  '''
  pass
add_body_class(MOOVBoxBody)

class MVHDBoxBody(FullBoxBody):
  ''' An 'mvhd' Movie Header box - ISO14496 section 8.2.2.
  '''

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, **kw)
    # obtain box data after version and flags decode
    if self.version == 0:
      self.add_from_buffer('creation_time', bfr, UInt32BE)
      self.add_from_buffer('modification_time', bfr, UInt32BE)
      self.add_from_buffer('timescale', bfr, UInt32BE)
      self.add_from_buffer('duration', bfr, UInt32BE)
    elif self.version == 1:
      self.add_from_buffer('creation_time', bfr, UInt64BE)
      self.add_from_buffer('modification_time', bfr, UInt64BE)
      self.add_from_buffer('timescale', bfr, UInt32BE)
      self.add_from_buffer('duration', bfr, UInt64BE)
    else:
      raise ValueError("MVHD: unsupported version %d" % (self.version,))
    self.add_from_buffer('rate_long', bfr, Int32BE)
    self.add_from_buffer('volume_short', bfr, Int16BE)
    self.add_from_buffer('reserved1', bfr, 10)      # 2-reserved, 2x4 reserved
    self.add_from_buffer('matrix', bfr, multi_struct_field('>lllllllll'))
    self.add_from_buffer('predefined1', bfr, 24)    # 6x4 predefined
    self.add_from_buffer('next_track_id', bfr, UInt32BE)

  @prop
  def rate(self):
    ''' Rate field converted to float: 1.0 represents normal rate.
    '''
    rate_long = self.rate_long
    return (rate_long>>16) + (rate_long&0xffff)/65536.0

  @prop
  def volume(self):
    ''' Volume field converted to float: 1.0 represents full volume.
    '''
    volume_short = self.volume_short
    return (volume_short>>8) + (volume_short&0xff)/256.0

add_body_class(MVHDBoxBody)

add_body_subclass(ContainerBoxBody, 'trak', '8.3.1', 'Track')

class TKHDBoxBody(FullBoxBody):
  ''' An 'tkhd' Track Header box - ISO14496 section 8.2.2.
  '''

  ATTRIBUTES = ( 'track_enabled',
                 'track_in_movie',
                 'track_in_preview',
                 'track_size_is_aspect_ratio',
                 'creation_time',
                 'modification_time',
                 'track_id',
                 'duration',
                 'layer',
                 'alternate_group',
                 'volume',
                 ('matrix', '%r'),
                 'width',
                 'height',
               )

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, **kw)
    # obtain box data after version and flags decode
    if self.version == 0:
      self.add_from_buffer('creation_time', bfr, UInt32BE)
      self.add_from_buffer('modification_time', bfr, UInt32BE)
      self.add_from_buffer('track_id', bfr, UInt32BE)
      self.add_from_buffer('reserved1', bfr, UInt32BE)
      self.add_from_buffer('duration', bfr, UInt32BE)
    elif self.version == 1:
      self.add_from_buffer('creation_time', bfr, UInt64BE)
      self.add_from_buffer('modification_time', bfr, UInt64BE)
      self.add_from_buffer('track_id', bfr, UInt32BE)
      self.add_from_buffer('reserved1', bfr, UInt32BE)
      self.add_from_buffer('duration', bfr, UInt64BE)
    else:
      raise ValueError("TRHD: unsupported version %d" % (self.version,))
    self.add_from_buffer('reserved2', bfr, UInt32BE)
    self.add_from_buffer('reserved3', bfr, UInt32BE)
    self.add_from_buffer('layer', bfr, Int16BE)
    self.add_from_buffer('alternate_group', bfr, Int16BE)
    self.add_from_buffer('volume', bfr, Int16BE)
    self.add_from_buffer('reserved4', bfr, UInt16BE)
    self.add_from_buffer('matrix', bfr, multi_struct_field('>lllllllll'))
    self.add_from_buffer('width', bfr, UInt32BE)
    self.add_from_buffer('height', bfr, UInt32BE)

  @prop
  def track_enabled(self):
    ''' Test flags bit 0, 0x1, track_enabled.
    '''
    return (self.flags&0x1) != 0

  @prop
  def track_in_movie(self):
    ''' Test flags bit 1, 0x2, track_in_movie.
    '''
    return (self.flags&0x2) != 0

  @prop
  def track_in_preview(self):
    ''' Test flags bit 2, 0x4, track_in_preview.
    '''
    return (self.flags&0x4) != 0

  @prop
  def track_size_is_aspect_ratio(self):
    ''' Test flags bit 3, 0x8, track_size_is_aspect_ratio.
    '''
    return (self.flags&0x8) != 0

add_body_class(TKHDBoxBody)

##add_body_subclass(ContainerBoxBody, 'tref', '8.3.3', 'track Reference')

class TREFBoxBody(ContainerBoxBody):
  ''' Track Reference BoxBody, container for trackReferenceTypeBoxes - ISO14496 section 8.3.3.
  '''

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, default_type=TrackReferenceTypeBoxBody, **kw)

add_body_class(TREFBoxBody)

class TrackReferenceTypeBoxBody(BoxBody):
  ''' A TrackReferenceTypeBoxBody contains references to other tracks - ISO14496 section 8.3.3.2.
  '''

  BOX_TYPES = (b'hint', b'cdsc', b'font', b'hind', b'vdep', b'vplx', b'subt')

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, **kw)
    track_bs = b''.join(self._skip_data(bfr))
    track_ids = []
    while not bfr.at_eof():
      track_ids.append(UInt32BE.from_buffer(bfr))
    self.add_field('track_id', ListField(track_ids))

add_body_class(TrackReferenceTypeBoxBody)
add_body_subclass(ContainerBoxBody, 'trgr', '8.3.4', 'Track Group')

class TrackGroupTypeBoxBody(FullBoxBody):
  ''' A TrackGroupTypeBoxBody contains track group id types - ISO14496 section 8.3.3.2.
  '''

  def __init__(self, box_type, box_data):
    FullBoxBody.__init__(self, box_type, box_data)

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, **kw)
    self.add_from_buffer('track_group_id', bfr, UInt32BE)

add_body_subclass(TrackGroupTypeBoxBody, 'msrc', '8.3.4.3', 'Multi-source presentation Track Group')
add_body_subclass(ContainerBoxBody, 'mdia', '8.4.1', 'Media')

class MDHDBoxBody(FullBoxBody):
  ''' A MDHDBoxBody is a Media Header box - ISO14496 section 8.4.2.
  '''

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, **kw)
    # obtain box data after version and flags decode
    if self.version == 0:
      self.add_from_buffer('creation_time', bfr, UInt32BE)
      self.add_from_buffer('modification_time', bfr, UInt32BE)
      self.add_from_buffer('timescale', bfr, UInt32BE)
      self.add_from_buffer('duration', bfr, UInt32BE)
    elif self.version == 1:
      self.add_from_buffer('creation_time', bfr, UInt64BE)
      self.add_from_buffer('modification_time', bfr, UInt64BE)
      self.add_from_buffer('timescale', bfr, UInt32BE)
      self.add_from_buffer('duration', bfr, UInt64BE)
    else:
      raise RuntimeError("unsupported version %d" % (self.version,))
    self.add_from_buffer('language_short', bfr, UInt16BE)
    self.add_from_buffer('pre_defined', bfr, UInt16BE)

  @prop
  def language(self):
    ''' The ISO 639‐2/T language code as decoded from the packed form.
    '''
    language_short = self.language_short
    return bytes([ x + 0x60
                   for x in ( (language_short>>10)&0x1f,
                              (language_short>>5)&0x1f,
                              language_short&0x1f
                            )
                 ]).decode('ascii')

add_body_class(MDHDBoxBody)

class HDLRBoxBody(FullBoxBody):
  ''' A HDLRBoxBody is a Handler Reference box - ISO14496 section 8.4.3.
  '''

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, **kw)
    # NB: handler_type is supposed to be an unsigned long, but in practice seems to be 4 ASCII bytes, so we load it as a string for readability
    self.add_from_buffer('pre_defined', bfr, UInt32BE)
    self.add_from_buffer('handler_type_long', bfr, UInt32BE)
    self.add_from_buffer('reserved1', bfr, UInt32BE)
    self.add_from_buffer('reserved2', bfr, UInt32BE)
    self.add_from_buffer('reserved3', bfr, UInt32BE)
    self.add_from_buffer('name', bfr, UTF8NULField)

  @property
  def handler_type(self):
    ''' The handler_type as an ASCII string, its usual form.
    '''
    return bytes(self.fields['handler_type_long']).decode('ascii')

add_body_class(HDLRBoxBody)
add_body_subclass(ContainerBoxBody, b'minf', '8.4.4', 'Media Information')
add_body_subclass(FullBoxBody, 'nmhd', '8.4.5.2', 'Null Media Header')

class ELNGBoxBody(FullBoxBody):
  ''' A ELNGBoxBody is a Extended Language Tag box - ISO14496 section 8.4.6.
  '''

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, **kw)
    # extended language based on RFC4646
    self.add_from_buffer('extended_language', bfr, UTF8NULField)
    ##lang_bs = b''.join(bfr)
    ##self.add_field('extended_language', BytesField(lang_bs))

add_body_class(ELNGBoxBody)
add_body_subclass(ContainerBoxBody, b'stbl', '8.5.1', 'Sample Table')

class _SampleTableContainerBoxBody(FullBoxBody):
  ''' An intermediate FullBoxBody subclass which contains more boxes.
  '''

  def parse_buffer(self, bfr, copy_boxes=None, **kw):
    super().parse_buffer(bfr, copy_boxes=copy_boxes, **kw)
    # obtain box data after version and flags decode
    entry_count = self.add_from_buffer('entry_count', bfr, UInt32BE)
    boxes = self.add_from_buffer(
        'boxes', bfr, SubBoxesField,
        end_offset=Ellipsis,
        max_boxes=entry_count,
        parent=self.box,
        copy_boxes=copy_boxes)
    if len(boxes) != entry_count:
      raise ValueError(
          "expected %d contained Boxes but parsed %d"
          % (entry_count, len(boxes)))

  def dump(self, indent='', fp=None):
    if fp is None:
      fp = sys.stdout
    fp.write(indent)
    fp.write(self.__class__.__name__)
    fp.write('\n')
    indent += '  '
    for B in self.boxes:
      B.dump(indent, fp)

add_body_subclass(_SampleTableContainerBoxBody, b'stsd', '8.5.2', 'Sample Description')

class _SampleEntry(BoxBody):
  ''' Superclass of Sample Entry boxes.
  '''

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, **kw)
    self.add_from_buffer('reserved', bfr, fixed_bytes_field(6))
    self.add_from_buffer('data_reference_index', bfr, UInt16BE)

class BTRTBoxBody(BoxBody):
  ''' BitRateBoxBody - section 8.5.2.2.
  '''

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, **kw)
    self.add_from_buffer('bufferSizeDB', bfr, UInt32BE)
    self.add_from_buffer('maxBitrate', bfr, UInt32BE)
    self.add_from_buffer('avgBitRate', bfr, UInt32BE)

add_body_class(BTRTBoxBody)
add_body_subclass(_SampleTableContainerBoxBody, b'stdp', '8.5.3', 'Degradation Priority')

TTSB_Sample = namedtuple('TTSB_Sample', 'count delta')

def add_generic_sample_boxbody(
    box_type, section, desc,
    struct_format_v0,
    sample_fields,
    struct_format_v1=None,
    has_inferred_entry_count=False,
):
  ''' Create and add a specific Time to Sample box - section 8.6.1.
  '''
  if struct_format_v1 is None:
    struct_format_v1 = struct_format_v0
  class_name = box_type.decode('ascii').upper() + 'BoxBody'
  sample_class_name = class_name + 'Sample'
  sample_type_v0 = structtuple(
      sample_class_name+ 'V0', struct_format_v0, sample_fields)
  sample_type_v1 = structtuple(
      sample_class_name+ 'V1', struct_format_v1, sample_fields)
  class SpecificSampleBoxBody(FullBoxBody):
    ''' Time to Sample box - section 8.6.1.
    '''
    def parse_buffer(self, bfr, **kw):
      super().parse_buffer(bfr, **kw)
      if self.version == 0:
        sample_type = self.sample_type = sample_type_v0
      elif self.version == 1:
        sample_type = self.sample_type = sample_type_v1
      else:
        warning("unsupported version %d, treating like version 1", self.version)
        sample_type = self.sample_type = sample_type_v1
      self.has_inferred_entry_count = has_inferred_entry_count
      if has_inferred_entry_count:
        remaining = (self.end_offset - bfr.offset)
        entry_count = remaining // S.size
        remainder = remaining % S.size
        if remainder != 0:
          warning("remaining length %d is not a multiple of len(%s), %d bytes left over",
                  remaining, S.size, remainder)
      else:
        entry_count = self.add_from_buffer('entry_count', bfr, UInt32BE)
      samples = []
      for _ in range(entry_count):
        samples.append(sample_type.from_buffer(bfr))
      self.add_field('samples', ListField(samples))
  SpecificSampleBoxBody.__name__ = class_name
  SpecificSampleBoxBody. __doc__ = (
      "Box type %r %s box - ISO14496 section %s."
      % (box_type, desc, section)
  )
  # we define these here because the names collide with the closure
  SpecificSampleBoxBody.struct_format_v0 = struct_format_v0
  SpecificSampleBoxBody.sample_type_v0 = sample_type_v0
  SpecificSampleBoxBody.struct_format_v1 = struct_format_v1
  SpecificSampleBoxBody.sample_type_v1 = sample_type_v1
  add_body_class(SpecificSampleBoxBody)
  return SpecificSampleBoxBody

def add_time_to_sample_boxbody(box_type, section, desc):
  ''' Add a Time to Sample box - section 8.6.1.
  '''
  return add_generic_sample_boxbody(
      box_type, section, desc,
      '>LL', 'count delta',
      has_inferred_entry_count=False,
  )

add_time_to_sample_boxbody(b'stts', '8.6.1.2.1', 'Time to Sample')

add_generic_sample_boxbody(
    b'ctts', '8.6.1.3', 'Composition Time to Sample',
    '>LL', 'count offset', '>Ll')

class CSLGBoxBody(FullBoxBody):
  ''' A 'cslg' Composition to Decode box - section 8.6.1.4.
  '''

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, **kw)
    if self.version == 0:
      struct_format = '>lllll'
    elif self.version == 1:
      struct_format = '>qqqqq'
    else:
      warning("unsupported version %d, treating like version 1")
      struct_format = '>qqqqq'
    self.add_field('fields',
        multi_struct_field(
            struct_format,
            (   'compositionToDTSShift',
                'leastDecodeToDisplayDelta',
                'greatestDecodeToDisplayDelta',
                'compositionStartTime',
                'compositionEndTime'
            )))

  @property
  def compositionToDTSShift(self):
    return self.fields.compositionToDTSShift

  @property
  def leastDecodeToDisplayDelta(self):
    return self.fields.leastDecodeToDisplayDelta

  @property
  def greatestDecodeToDisplayDelta(self):
    return self.fields.greatestDecodeToDisplayDelta

  @property
  def compositionStartTime(self):
    return self.fields.compositionStartTime

  @property
  def compositionEndTime(self):
    return self.fields.compositionEndTime

add_body_class(CSLGBoxBody)

add_generic_sample_boxbody(
    b'stss', '8.6.2', 'Sync Sample',
    '>L', 'number')

add_generic_sample_boxbody(
    b'stsh', '8.6.3', 'Shadow Sync Table',
    '>LL', 'shadowed_sample_number sync_sample_number')

add_generic_sample_boxbody(
    b'sdtp', '8.6.4', 'Independent and Disposable Samples',
    '>HHHH',
    'is_leading sample_depends_on sample_is_depended_on sample_has_redundancy',
    has_inferred_entry_count=True)

add_body_subclass(BoxBody, b'edts', '8.6.5.1', 'Edit')

add_generic_sample_boxbody(
    b'elst', '8.6.6', 'Edit List',
    '>Ll', 'segment_duration media_time', '>Qq')

add_body_subclass(BoxBody, b'dinf', '8.7.1', 'Data Information')

class URL_BoxBody(FullBoxBody):
  ''' An 'url ' Data Entry URL BoxBody - section 8.7.2.1.
  '''

  def parse_data(self, bfr, **kw):
    super().parse_data(bfr, **kw)
    self.add_from_buffer('location', bfr, UTF8NULField)

add_body_class(URL_BoxBody)

class URN_BoxBody(FullBoxBody):
  ''' An 'urn ' Data Entry URL BoxBody - section 8.7.2.1.
  '''

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, **kw)
    self.add_from_buffer('name', bfr, UTF8NULField)
    self.add_from_buffer('location', bfr, UTF8NULField)

add_body_class(URN_BoxBody)

class STSZBoxBody(FullBoxBody):
  ''' A 'stsz' Sample Size box - section 8.7.3.2.
  '''

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, **kw)
    sample_size = self.add_from_buffer('sample_size', bfr, UInt32BE)
    sample_count = self.add_from_buffer('sample_count', bfr, UInt32BE)
    if sample_size == 0:
      entry_sizes = []
      for _ in range(sample_count):
        entry_sizes.append(UInt32BE.from_buffer(bfr))
      self.add_field('entry_sizes', ListField(entry_sizes))

add_body_class(STSZBoxBody)

class STZ2BoxBody(FullBoxBody):
  ''' A 'stz2' Compact Sample Size box - section 8.7.3.3.
  '''

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, **kw)
    self.add_field('reserved', fixed_bytes_field(3).from_buffer(bfr))
    field_size = self.add_from_buffer('field_size', bfr, UInt8)
    sample_count = self.add_from_buffer('sample_count', bfr, UInt32BE)
    entry_sizes = []
    if field_size == 4:
      # nybbles packed into bytes
      for i in range(sample_count):
        if i % 2 == 0:
          bs = bfr.take(1)
          entry_sizes.append(bs[0] >> 4)
        else:
          entry_sizes.append(bs[0] & 0x0f)
      self.add_field('entry_sizes', ListField(entry_sizes))
    elif field_size == 8:
      for _ in range(sample_count):
        entry_sizes.append(UInt8.from_buffer(bfr))
      self.add_field('entry_sizes', ListField(entry_sizes))
    elif field_size == 16:
      for _ in range(sample_count):
        entry_sizes.append(UInt16BE.from_buffer(bfr))
      self.add_field('entry_sizes', ListField(entry_sizes))
    else:
      warning("unhandled field_size=%s, not parsing entry_sizes", field_size)

class STSCBoxBody(FullBoxBody):
  ''' 'stsc' (Sample Table box - section 8.7.4.1.
  '''

  STSCEntry = structtuple('STSCEntry', '>LLL', 'first_chunk samples_per_chunk sample_description_index')

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, **kw)
    entry_count = self.add_from_buffer('entry_count', bfr, UInt32BE)
    entries = []
    for _ in range(entry_count):
      entries.append(STSCBoxBody.STSCEntry.from_buffer(bfr))
    self.add_field('entries', ListField(entries))

add_body_class(STSCBoxBody)

class STCOBoxBody(FullBoxBody):
  ''' A 'stco' Chunk Offset box - section 8.7.5.
  '''

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, **kw)
    entry_count = self.add_from_buffer('entry_count', bfr, UInt32BE)
    chunk_offsets = []
    for _ in range(entry_count):
      chunk_offsets.append(UInt32BE.from_buffer(bfr))
    self.add_field('chunk_offsets', ListField(chunk_offsets))

add_body_class(STCOBoxBody)

class CO64BoxBody(FullBoxBody):
  ''' A 'c064' Chunk Offset box - section 8.7.5.
  '''

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, **kw)
    entry_count = self.add_from_buffer('entry_count', bfr, UInt32BE)
    chunk_offsets = []
    for _ in range(entry_count):
      chunk_offsets.append(UInt64BE.from_buffer(bfr))
    self.add_field('chunk_offsets', ListField(chunk_offsets))

add_body_class(CO64BoxBody)

class DREFBoxBody(FullBoxBody):
  ''' A 'dref' Data Reference box containing Data Entry boxes - section 8.7.2.1.
  '''

  def parse_buffer(self, bfr, copy_boxes=None, **kw):
    super().parse_buffer(bfr, copy_boxes=copy_boxes, **kw)
    entry_count = self.add_from_buffer('entry_count', bfr, UInt32BE)
    boxes = self.add_from_buffer(
        'boxes', bfr, SubBoxesField,
        end_offset=Ellipsis, max_boxes=entry_count, parent=self.box,
        copy_boxes=copy_boxes)

add_body_class(DREFBoxBody)

add_body_subclass(ContainerBoxBody, b'udta', '8.10.1', 'User Data')

class METABoxBody(FullBoxBody):
  ''' A 'meta' Meta BoxBody - section 8.11.1.
  '''

  def parse_buffer(self, bfr, copy_boxes=None, **kw):
    super().parse_buffer(bfr, copy_boxes=copy_boxes, **kw)
    theHandler = self.add_field('theHandler', Box.from_buffer(bfr))
    theHandler.parent = self.box
    self.add_from_buffer(
        'boxes', bfr, SubBoxesField,
        end_offset=Ellipsis, parent=self.box,
        copy_boxes=copy_boxes)

add_body_class(METABoxBody)

class VMHDBoxBody(FullBoxBody):
  ''' A 'vmhd' Video Media Headerbox - section 12.1.2.
  '''

  OpColor = multi_struct_field('>HHH', class_name='OpColor')

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, **kw)
    self.add_from_buffer('graphicsmode', bfr, UInt16BE)
    self.add_from_buffer('opcolor', bfr, VMHDBoxBody.OpColor)

add_body_class(VMHDBoxBody)

class SMHDBoxBody(FullBoxBody):
  ''' A 'smhd' Sound Media Headerbox - section 12.2.2.
  '''

  def parse_buffer(self, bfr, **kw):
    super().parse_buffer(bfr, **kw)
    self.add_from_buffer('balance', bfr, Int16BE)
    self.add_from_buffer('reserved', bfr, UInt16BE)

add_body_class(SMHDBoxBody)

def parse(o, **kw):
  ''' Yield top level Boxes from a source (str, int, file).
  '''
  close = None
  with Pfx("parse(%r)", o):
    if isinstance(o, str):
      fd = os.open(o, os.O_RDONLY)
      parser = parse_fd(fd, **kw)
      close = partial(os.close, fd)
    elif isinstance(o, int):
      parser = parse_fd(o, **kw)
    else:
      parser = parse_file(o, **kw)
    yield from parser
    if close:
      close()

def parse_fd(fd, **kw):
  ''' Parse an ISO14496 stream from the file descriptor `fd`, yield top level Boxes.
      `fd`: a file descriptor open for read
      `discard_data`: whether to discard unparsed data, default False
      `copy_offsets`: callable to receive BoxBody offsets
  '''
  return parse_buffer(CornuCopyBuffer.from_fd(fd), **kw)

def parse_file(fp, **kw):
  ''' Parse an ISO14496 stream from the file `fp`, yield top level Boxes.
      `fp`: a file open for read
      `discard_data`: whether to discard unparsed data, default False
      `copy_offsets`: callable to receive BoxBody offsets
  '''
  return parse_buffer(CornuCopyBuffer.from_file(fp), **kw)

def parse_chunks(chunks, **kw):
  ''' Parse an ISO14496 stream from the iterabor of data `chunks`, yield top level Boxes.
      `chunks`: an iterator yielding bytes objects
      `discard_data`: whether to discard unparsed data, default False
      `copy_offsets`: callable to receive BoxBody offsets
  '''
  return parse_buffer(CornuCopyBuffer(chunks), **kw)

def parse_buffer(bfr, copy_offsets=None, **kw):
  ''' Parse an ISO14496 stream from the CornuCopyBuffer `bfr`, yield top level Boxes.
      `bfr`: a CornuCopyBuffer provided the stream data, preferably seekable
      `discard_data`: whether to discard unparsed data, default False
      `copy_offsets`: callable to receive Box offsets
  '''
  if copy_offsets is not None:
    bfr.copy_offsets = copy_offsets
  while not bfr.at_eof():
    yield Box.from_buffer(bfr, **kw)

if __name__ == '__main__':
  sys.exit(main(sys.argv))
