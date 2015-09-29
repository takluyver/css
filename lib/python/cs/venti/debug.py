#!/usr/bin/python

import sys
from cs.lex import hexify
from cs.logutils import X

def dump_Block(block, indent=''):
  X("%s%s %s %d bytes",
    indent,
    hexify(block.hashcode()),
    "indirect" if block.indirect else "direct",
    len(block))
  if block.indirect:
    indent += '  '
    subblocks = block.subblocks()
    X("%sindirect %d subblocks, span %d bytes",
      indent, len(subblocks), len(block))
    for B in subblocks:
      dump_Block(B, indent=indent)

def dump_Dirent(E, indent='', recurse=False):
  X("%s%r %s", indent, hexify(E.block.hashcode()))
  if E.isdir:
    indent += '  '
    for name in sorted(D.keys()):
      E2 = D[name]
      if recurse:
        dump_Dirent(E2, indent, recurse=True)
      else:
        X("%s%s %r %s",
          indent,
          'd' if E2.isdir else '-',
          E.name,
          E.block.hashcode())
