#!/usr/bin/env python
#
# Web page utility.
#       - Cameron Simpson <cs@zip.com.au> 07jul2010
#

from __future__ import with_statement, print_function
import sys
import os
import errno
import os.path
import shlex
from collections import defaultdict
from copy import copy
from functools import partial
from itertools import chain
import re
if sys.hexversion < 0x02060000: from sets import Set as set
from getopt import getopt, GetoptError
from string import Formatter
from time import sleep
from threading import Lock, Thread
from urllib import quote, unquote
from urllib2 import HTTPError, URLError, build_opener, HTTPBasicAuthHandler
try:
  import xml.etree.cElementTree as ElementTree
except ImportError:
  import xml.etree.ElementTree as ElementTree
from cs.debug import thread_dump
from cs.fileutils import file_property
from cs.later import Later, FUNC_ONE_TO_ONE, FUNC_ONE_TO_MANY, FUNC_SELECTOR, FUNC_MANY_TO_MANY
from cs.lex import get_identifier
from cs.logutils import setup_logging, logTo, Pfx, debug, error, warning, exception, trace, pfx_iter, D
from cs.queues import IterableQueue, NullQueue, NullQ
from cs.threads import locked_property
from cs.urlutils import URL, NetrcHTTPPasswordMgr
from cs.obj import O
from cs.py3 import input, ConfigParser

if os.environ.get('DEBUG', ''):
  def X(tag, *a):
    D("TRACE: "+tag, *a)
else:
  def X(*a):
    pass

ARCHIVE_SUFFIXES = ( 'tar', 'tgz', 'tar.gz', 'tar.bz2', 'cpio', 'rar', 'zip', 'dmg' )
IMAGE_SUFFIXES = ( 'png', 'jpg', 'jpeg', 'gif', 'ico', )
VIDEO_SUFFIXES = ( 'mp2', 'mp4', 'avi', 'wmv', )

DEFAULT_JOBS = 4

usage = '''Usage: %s [options...] op [args...]
  %s url URL actions...
      URL may be "-" to read URLs from standard input.
  Options:
    -j jobs
        How many jobs (URL fetches, minor computations) to run at a time.
        Default: %d
    -q  Quiet. Don't recite surviving URLs at the end.
    -u  Unbuffered. Flush print actions as they occur.'''

def main(argv):
  argv = list(argv)
  xit = 0
  argv0 = argv.pop(0)
  cmd = os.path.basename(argv0)
  setup_logging(cmd)
  logTo('.pilfer.log')

  P = Pilfer()
  quiet = False
  jobs = DEFAULT_JOBS

  badopts = False

  try:
    opts, argv = getopt(argv, 'j:qu')
  except GetoptError as e:
    warning("%s", e)
    badopts = True
    opts = ()

  for opt, val in opts:
    with Pfx("%s", opt):
      if opt == '-j':
        jobs = int(val)
      elif opt == '-q':
        quiet = True
      elif opt == '-u':
        P.flush_print = True
      else:
        raise NotImplementedError("unimplemented option")

  if not argv:
    error("missing op")
    badopts = True
  else:
    op = argv.pop(0)
    if op.startswith('http://') or op.startswith('https://') :
      # push the URL back and infer missing "url" op word
      argv.insert(0, op)
      op ='url'
    with Pfx(op):
      if op == 'url':
        if not argv:
          error("missing URL")
          badopts = True
        else:
          url = argv.pop(0)

          # load any named pipeline definitions on the command line
          while True:
            pipe_name, pipe_funcs, argv2, errors = get_pipeline_spec(argv)
            if pipe_name is None:
              if errors:
                raise RuntimeError("pipe_name is None but errors=%r", errors)
              if pipe_funcs:
                raise RuntimeError("pipe_name is None but pipe_funcs=%r", pipe_funcs)
              break
            argv = argv2
            with Pfx("pipe %s", pipe_name):
              if errors:
                badopts = True
                for err in errors:
                  error(err)
              else:
                try:
                  P.add_pipeline(pipe_name, pipe_funcs)
                except KeyError as e:
                  error("add_pipeline: %s", e)
                  badopts = True

          # gather up the remaining definition as the runing pipeline
          pipe_funcs, errors = argv_pipefuncs(argv2)

          # report accumulated errors and set badopts
          if errors:
            for err in errors:
              error(err)
            badopts = True
          if not badopts:
            with Later(jobs) as L:
              P.later = L
              # commence the main pipeline by converting strings to URL objects
              # and associating the initial Pilfer object as scope
              def add_scope(U):
                return P, URL(U, None, scope=P)
              pipe_funcs.insert(0, (FUNC_ONE_TO_ONE, add_scope))
              # construct the pipeline
              inQ, outQ = L.pipeline(pipe_funcs, outQ=NullQueue(blocking=True, open=True))
              if url != '-':
                # literal URL supplied, deliver to pipeline
                inQ.put(url)
              else:
                # read URLs from stdin
                try:
                  do_prompt = sys.stdin.isatty()
                except AttributeError:
                  do_prompt = False
                if do_prompt:
                  # interactively prompt for URLs, deliver to pipeline
                  prompt = cmd + ".url> "
                  while True:
                    try:
                      url = input(prompt)
                    except EOFError:
                      break
                    else:
                      inQ.put(url)
                else:
                  # read URLs from non-interactive stdin, deliver to pipeline
                  lineno = 0
                  for line in sys.stdin:
                    lineno += 1
                    with Pfx("stdin:%d", lineno):
                      if not line.endswith('\n'):
                        raise ValueError("unexpected EOF - missing newline")
                      line = line.strip()
                      if not line or line.startswith('#'):
                        debug("SKIP: %s", line)
                        continue
                      inQ.put(url)
              # indicate end of input
              inQ.close()
              # await processing of output
              for item in outQ:
                warning("MAIN: finalisation collected %r", item)
      else:
        error("unsupported op")
        badopts = True

  if badopts:
    print(usage % (cmd, cmd, DEFAULT_JOBS), file=sys.stderr)
    xit = 2

  return xit

def argv_pipefuncs(argv):
  ''' Process command line strings and return a corresponding list
      of functions to construct a Later.pipeline.
  '''
  debug("argv_pipefuncs(%r)", argv)
  errors = []
  pipe_funcs = []
  for action in argv:
    try:
      func_sig, function = action_func(action)
    except ValueError as e:
      errors.append(str(e))
    else:
      pipe_funcs.append( (func_sig, function) )
  return pipe_funcs, errors

def get_pipeline_spec(argv):
  ''' Parse a leading pipeline specification from the list of strings `argv`.
      Return (pipe_name, pipe_funcs, argv2, errors) where pipe_name is the
      name of the pipe specification, pipe_funcs is a list of
      (func_sig, function) pairs and `argv2` is the remaining list.
      If there is no leading pipeline specification return None for
      pipe_name and pipe_funcs.
      Return parse errors in `errors`.
  '''
  errors = []
  pipe_name = None
  pipe_funcs = None
  if not argv:
    # no arguments, no spec
    argv2 = argv
  else:
    arg = argv[0]
    if not arg.endswith(':{'):
      # not a start-of-spec
      argv2 = argv
    else:
      pipe_name, offset = get_identifier(arg)
      if not pipe_name or offset != len(arg)-2:
        # still not a start-of-spec
        argv2 = argv
      else:
        with Pfx(arg):
          # started with "foo:{"; gather spec until "}"
          for i in range(1, len(argv)):
            if argv[i] == '}':
              pipe_funcs, pferrors = argv_pipefuncs(argv[1:i])
              errors.extend(pferrors)
              argv2 = argv[i+1:]
              break
          if pipe_funcs is None:
            errors.append('%s: missing closing "}"' % (arg,))
            argv2 = argv[1:]
  return pipe_name, pipe_funcs, argv2, errors

def notNone(v, name="value"):
  if v is None:
    raise ValueError("%s is None" % (name,))
  return True

def url_xml_find(U, match):
  for found in url_io(URL(U, None).xmlFindall, (), match):
    yield ElementTree.tostring(found, encoding='utf-8')

def unique(items, seen=None):
  ''' A generator that yields unseen items, as opposed to just
      stuffing them all into a set and returning the set.
  '''
  if seen is None:
    seen = set()
  for I in items:
    if I not in seen:
      yield I
      seen.add(I)

class PilferCommon(O):
  ''' Common state associated with all Pilfers.
      Pipeline definitions, seen sets, etc.
  '''

  def __init__(self):
    O.__init__(self)
    self.seen = defaultdict(set)
    self.pipe_queues = {}       # mapping of names to PipeLines
    self.opener = build_opener()
    self.opener.add_handler(HTTPBasicAuthHandler(NetrcHTTPPasswordMgr()))

class Pilfer(O):
  ''' State for the pilfer app.
      Notable attribute include:
        .flush_print    Flush output after print(), default False.
        .user_agent     Specify user-agent string, default None.
        .user_vars      Mapping of user variables for arbitrary use.
  '''

  def __init__(self, **kw):
    self._lock = Lock()
    self.flush_print = False
    self._print_to = None
    self._print_lock = Lock()
    self.user_agent = None
    self.user_vars = {}
    self._urlsfile = None
    O.__init__(self, **kw)
    if not hasattr(self, '_shared'):
      self._shared = PilferCommon()                  # common state - seen URLs, etc

  def __copy__(self):
    ''' Copy this Pilfer state item, preserving shared state.
    '''
    return Pilfer(user_vars=dict(self.user_vars),
                  _shared=self._shared,
                 )

  def seen(self, url, seenset='_'):
    return url in self._shared.seen[seenset]

  def see(self, url, seenset='_'):
    self._shared.seen[seenset].add(url)

  @property
  def pipe_queues(self):
    return self._shared.pipe_queues

  def add_pipeline(self, pipe_name, pipe_funcs):
    pqs = self.pipe_queues
    if pipe_name in pqs:
      raise KeyError("pipe %r already defined" % (pipe_name,))
    pqs[pipe_name] = PipeLine(pipe_name, pipe_funcs, self)

  def _print(self, *a, **kw):
    file = kw.pop('file', None)
    if kw:
      raise ValueError("unexpected kwargs %r" % (kw,))
    with self._print_lock:
      if file is None:
        file = self._print_to if self._print_to else sys.stdout
      print(*a, file=file)
      if self.flush_print:
        file.flush()

  def set_user_vars(self, **kw):
    ''' Update self.user_vars from the keyword arguments.
    '''
    self.user_vars.update(kw)

  def print_url_string(self, U, **kw):
    ''' Print a string using approved URL attributes as the format dictionary.
        See Pilfer.format_string.
    '''
    print_string = kw.pop('string', '{url}')
    print_string = self.format_string(print_string, U)
    file = kw.pop('file', self._print_to)
    if kw:
      warning("print_url_string: unexpected keyword arguments: %r", kw)
    self._print(print_string, file=file)

  def save_url(self, U, saveas=None, dir=None, overwrite=False, **kw):
    ''' Save the contents of the URL `U`.
    '''
    debug("save_url(U=%r, saveas=%r, dir=%s, overwrite=%r, kw=%r)...", U, saveas, dir, overwrite, kw)
    with Pfx("save_url(%s)", U):
      save_dir = self.user_vars.get('save_dir', '.')
      if saveas is None:
        saveas = os.path.join(save_dir, U.basename)
        if saveas.endswith('/'):
          saveas += 'index.html'
      if saveas == '-':
        sys.stdout.write(U.content)
        sys.stdout.flush()
      else:
        with Pfx(saveas):
          if not overwrite and os.path.exists(saveas):
            warning("file exists, not saving")
          else:
            content = U.content
            try:
              with open(saveas, "wb") as savefp:
                savefp.write(content)
            except:
              exception("save fails")

  class Variables(object):
    ''' A mapping object to set or fetch user variables or URL attributes.
        Various URL attributes are known, and may not be assigned to.
        This mapping is used with str.format to fill in {value}s.
    '''

    _approved = (
                  'archives',
                  'basename',
                  'dirname',
                  'hrefs',
                  'images',
                  'parent',
                  'path',
                  'referer',
                  'srcs',
                  'title',
                  'url',
                  'videos',
                )

    def __init__(self, U):
      self.url = U

    def keys(self):
      return set(Pilfer.URLkeywords._approved) + set(self.user_vars.keys())

    def __getitem__(self, k):
      url = self.url
      with Pfx(url):
        if k in self._approved:
          if k == 'url':
            return url
          try:
            return getattr(url, k)
          except AttributeError as e:
            raise KeyError("no such attribute: .%s (%s)" % (k, e))
        else:
          return url.user_vars[k]

    def __setitem__(self, k, value):
      url = self.url
      with Pfx(url):
        if k in self._approved:
          raise KeyError("it is forbidden to assign to attribute .%s" % (k,))
        else:
          url.user_vars[k] = value

  def format_string(self, s, U):
    ''' Format a string using the URL as context.
    '''
    return Formatter().vformat(s, (), self.Variables(U))

  def set_user_var(self, k, value, U, raw=False):
    if not raw:
      value = self.format_string(value, U)
    self.Variables(U)[k] = value

def new_dir(self, dir):
  ''' Create the directory `dir` or `dir-n` if `dir` exists.
      Return the path of the directory created.
  '''
  try:
    os.makedirs(dir)
  except OSError as e:
    if e.errno != errno.EEXIST:
      raise
    n = 2
    while True:
      ndir = "%s-%d" % (dir, n)
      try:
        os.makedirs(ndir)
      except OSError as e:
        if e.errno != errno.EEXIST:
          raise
        n += 1
        continue
      dir = ndir
      break
  return dir

def has_exts(U, suffixes, case_sensitive=False):
  ''' Test if the .path component of a URL ends in one of a list of suffixes.
      Note that the .path component does not include the query_string.
  '''
  ok = False
  path = U.path
  if not path.endswith('/'):
    base = os.path.basename(path)
    if not case_sensitive:
      base = base.lower()
      suffixes = [ sfx.lower() for sfx in suffixes ]
    for sfx in suffixes:
      if base.endswith('.'+sfx):
        ok = True
        break
  return ok

def with_exts(urls, suffixes, case_sensitive=False):
  for U in urls:
    ok = False
    path = U.path
    if not path.endswith('/'):
      base = os.path.basename(path)
      if not case_sensitive:
        base = base.lower()
        suffixes = [ sfx.lower() for sfx in suffixes ]
      for sfx in suffixes:
        if base.endswith('.'+sfx):
          ok = True
          break
    if ok:
      yield U
    else:
      debug("with_exts: discard %s", U)

def substitute( (P, src), regexp, replacement, replace_all):
  ''' Perform a regexp substitution on `src`.
      `replacement` is a format string for the replacement text
      using the str.format method.
      The matched groups from the regexp take the positional arguments 1..n,
      with 0 used for the whole matched string.
      The keyword arguments consist of '_' for the whole matched string
      and any named groups.
  '''
  debug("SUBSTITUTE: src=%r, regexp=%r, replacement=%r, replace_all=%s)...",
        src, regexp.pattern, replacement, replace_all)
  strs = []
  sofar = 0
  for m in regexp.finditer(src):
    repl_args = [ m.group(0) ] + list(m.groups())
    repl_kw = { '_': m.group(0) }
    repl_kw.update(m.groupdict())
    strs.append(src[sofar:m.start()])
    strs.append(replacement.format(*repl_args, **repl_kw))
    sofar = m.end()
    if not replace_all:
      break
  strs.append(src[sofar:])
  result = ''.join(strs)
  debug("SUBSTITUTE: src=%r, result=%r", src, result)
  return result

def url_delay(U, delay, *a):
  sleep(float(delay))
  yield U

def url_query(U, *a):
  U = URL(U, None)
  if not a:
    yield U.query
  qsmap = dict( [ ( qsp.split('=', 1) if '=' in qsp else (qsp, '') ) for qsp in U.query.split('&') ] )
  yield ','.join( [ unquote(qsmap.get(qparam, '')) for qparam in a ] )

def url_io(func, onerror, *a, **kw):
  ''' Call `func` and return its result.
      If it raises URLError or HTTPError, report the error and return `onerror`.
  '''
  debug("url_io(%s, %s, %s, %s)...", func, onerror, a, kw)
  try:
    return func(*a, **kw)
  except (URLError, HTTPError) as e:
    warning("%s", e)
    return onerror

def url_io_iter(I):
  ''' Generator that calls `I.next()` until StopIteration, yielding
      its values.
      If the call raises URLError or HTTPError, report the error
      instead of aborting.
  '''
  while True:
    try:
      item = I.next()
    except StopIteration:
      break
    except (URLError, HTTPError) as e:
      warning("%s", e)
    else:
      yield item

def url_hrefs(U, referrer=None):
  ''' Yield the HREFs referenced by a URL.
      Conceals URLError, HTTPError.
  '''
  return url_io_iter(URL(U, referrer).hrefs(absolute=True))

def url_srcs(U, referrer=None):
  ''' Yield the SRCs referenced by a URL.
      Conceals URLError, HTTPError.
  '''
  return url_io_iter(URL(U, referrer).srcs(absolute=True))

# actions that work on the whole list of in-play URLs
many_to_many = {
      'sort':         lambda Ps, Us, *a, **kw: sorted(Us, *a, **kw),
      'unique':       lambda Ps, Us: unique(Us),
      'first':        lambda Ps, Us: Us[:1],
      'last':         lambda Ps, Us: Us[-1:],
    }

one_to_many = {
      'hrefs':        lambda (P, U), *a: url_hrefs(U, *a),
      'images':       lambda (P, U), *a: with_exts(url_hrefs(U, *a), IMAGE_SUFFIXES ),
      'iimages':      lambda (P, U), *a: with_exts(url_srcs(U, *a), IMAGE_SUFFIXES ),
      'srcs':         lambda (P, U), *a: url_srcs(U, *a),
      'xml':          lambda (P, U), match: url_xml_find(U, match),
      'xmltext':      lambda (P, U), match: XML(U).findall(match),
    }

# actions that work on individual URLs
one_to_one = {
      '..':           lambda (P, U): URL(U, None).parent,
      'delay':        lambda (P, U), delay: (U, sleep(float(delay)))[0],
      'domain':       lambda (P, U): URL(U, None).domain,
      'hostname':     lambda (P, U): URL(U, None).hostname,
      'per':          lambda (P, U): (copy(P), U),
      'print':        lambda (P, U), **kw: (U, P.print_url_string(U, **kw))[0],
      'query':        lambda (P, U), *a: url_query(U, *a),
      'quote':        lambda (P, U): quote(U),
      'unquote':      lambda (P, U): unquote(U),
      'save':         lambda (P, U), *a, **kw: (U, P.save_url(U, *a, **kw))[0],
      'see':          lambda (P, U): (U, P.see(U))[0],
      's':            substitute,
      'title':        lambda (P, U): U.title,
      'type':         lambda (P, U): url_io(U.content_type, ""),
      'xmlattr':      lambda (P, U), attr: [ A for A in (ElementTree.XML(U).get(attr),) if A is not None ],
    }
one_to_one_scoped = ('per',)

one_test = {
      'has_title':    lambda (P, U): U.title is not None,
      'is_archive':   lambda (P, U): has_exts( U, ARCHIVE_SUFFIXES ),
      'is_archive':   lambda (P, U): has_exts( U, ARCHIVE_SUFFIXES ),
      'is_image':     lambda (P, U): has_exts( U, IMAGE_SUFFIXES ),
      'is_video':     lambda (P, U): has_exts( U, VIDEO_SUFFIXES ),
      'reject_re':    lambda (P, U), regexp: not regexp.search(U),
      'same_domain':  lambda (P, U): notNone(U.referer, "U.referer") and U.domain == U.referer.domain,
      'same_hostname':lambda (P, U): notNone(U.referer, "U.referer") and U.hostname == U.referer.hostname,
      'same_scheme':  lambda (P, U): notNone(U.referer, "U.referer") and U.scheme == U.referer.scheme,
      'seen':         lambda (P, U): P.seen(U),
      'select_re':    lambda (P, U), regexp: regexp.search(U),
      'unseen':       lambda (P, U): not P.seen(U),
    }

re_COMPARE = re.compile(r'([a-z]\w*)==')
re_ASSIGN  = re.compile(r'([a-z]\w*)=')

def action_func(action):
  ''' Accept a string `action` and return a tuple of:
        func_sig, function
      `func_sig` and `function` are used with Later.pipeline
      and `kwargs` is used as extra parameters for `function`.
  '''
  function = None
  func_sig = None
  scoped = False        # function output is (P,U), not just U
  args = []             # collect foo and foo=bar operator arguments
  kwargs = {}
  # parse action into function and kwargs
  with Pfx("%s", action):
    action0 = action

    # comparison
    # varname==
    m = re_COMPARE.match(action)
    if m:
      kw_var = m.group(1)
      kw_value = action[m.end():]
      function = lambda (P, U): kw_var in P.user_vars and P.user_vars[kw_var] == U.format(kw_value, U)
      def function(P, U):
        D("compare user_vars[%s]...", kw_var)
        uv = P.user_vars
        if kw_var not in uv:
          return False
        v = U.format(kw_value, U)
        D("compare user_vars[%s]: %r => %r", kw_var, kw_value, v)
        return uv == v
      func_sig = FUNC_SELECTOR
    else:
      # assignment
      # varname=
      m = re_ASSIGN.match(action)
      if m:
        kw_var = m.group(1)
        kw_value = action[m.end():]
        def function(P, U):
          P.set_user_var(kw_var, kw_value, U)
          return U
        func_sig = FUNC_ONE_TO_ONE
      else:
        # operator or s//
        func, offset = get_identifier(action)
        if func:
          with Pfx(func):
            # an identifier
            if func == 's':
              # s/this/that/
              if offset == len(action):
                raise ValueError("missing delimiter")
              delim = action[offset]
              delim2pos = action.find(delim, offset+1)
              if delim2pos < offset + 1:
                raise ValueError("missing second delimiter (%r)" % (delim,))
              regexp = action[offset+1:delim2pos]
              if not regexp:
                raise ValueError("empty regexp")
              delim3pos = action.find(delim, delim2pos+1)
              if delim3pos < delim2pos+1:
                raise ValueError("missing third delimiter (%r)" % (delim,))
              repl_format = action[delim2pos+1:delim3pos]
              offset = delim3pos + 1
              repl_all = False
              repl_icase = False
              re_flags = 0
              while offset < len(action):
                modchar = action[offset]
                offset += 1
                if modchar == 'g':
                  repl_all = True
                elif modchar == 'i':
                  repl_icase = True
                  re_flags != re.IGNORECASE
                else:
                  raise ValueError("unknown s///x modifier: %r" % (modchar,))
              debug("s: regexp=%r, replacement=%r, repl_all=%s, repl_icase=%s", regexp, repl_format, repl_all, repl_icase)
              kwargs['regexp'] = re.compile(regexp, flags=re_flags)
              kwargs['replacement'] = repl_format
              kwargs['replace_all'] = repl_all
            elif func == "divert" or func == "pipe":
              # divert:pipe_name[:selector]
              # pipe:pipe_name[:selector]
              #
              # Divert selected items to the named pipeline
              # or filter selected items through an instance of the named pipeline.
              if offset == len(action):
                raise ValueError("missing marker")
              marker = action[offset]
              offset += 1
              pipe_name, offset = get_identifier(action, offset)
              if not pipe_name:
                raise ValueError("no pipe name")
              if offset < len(action):
                if marker != action[offset]:
                  raise ValueError("expected second marker to match first: expected %r, saw %r"
                                   % (marker, action[offset]))
                offset += 1
                raise RuntimeError("selector_func parsing not implemented")
              else:
                select_func = lambda (P, U): True
              do_divert = func == "divert"
              if do_divert:
                # function to divert selected items to a single named pipeline
                func_sig = FUNC_ONE_TO_MANY
                def function(P, U):
                  if select_func(P, U):
                    try:
                      pipe = P.pipe_queues[pipe_name].pipeline
                    except KeyError:
                      error("no pipe named %r", pipe_name)
                    else:
                      pipe.inQ.put( (P, U) )
                  else:
                    yield U
              else:
                ## TODO: present as a one-to-many function
                ##       yielding nothing but putting each
                ##       item onto the pipeline
                ##
                # A pipe runs all the items in this stream through
                # an instance of the named pipeline.
                # As such it is a many-to-many function.
                scoped = True
                func_sig = FUNC_MANY_TO_MANY
                def function(items):
                  if not isinstance(items, list):
                    items = list(utems)
                  try:
                    D("pipe:%s: items=%r", pipe_name, items)
                    if items:
                      P = items[0][0]
                      PL = P.pipe_queues[pipe_name].new_pipeline(inputs=items)
                      D("pipe:%s: PL=%r", pipe_name, PL)
                      for item in PL.outQ:
                        D("pipe:%s: yield %r", pipe_name, item)
                        yield item
                      D("pipe:%s: COMPLETE", pipe_name)
                  except Exception as e:
                    exception("pipe: %s", e)
                    raise
            elif offset < len(action):
              marker = action[offset]
              if marker == ':':
                # followed by :kw1=value,kw2=value,...
                kwtext = action[offset+1:]
                if func == "print":
                  # print is special - just a format string relying on current state
                  kwargs['string'] = kwtext
                else:
                  for kw in kwtext.split(','):
                    if '=' in kw:
                      kw, v = kw.split('=', 1)
                      kwargs[kw] = v
                    else:
                      args.append(kw)
              else:
                raise ValueError("unrecognised marker %r" % (marker,))
          if not function:
            if func_sig is not None:
              raise RuntimeError("func_sig is set (%r) but function is None" % (func_sig,))
            if func in many_to_many:
              # many-to-many functions get passed straight in
              function = many_to_many[func]
              func_sig = FUNC_MANY_TO_MANY
            elif func in one_to_many:
              function = one_to_many[func]
              func_sig = FUNC_ONE_TO_MANY
            elif func in one_to_one:
              function = one_to_one[func]
              func_sig = FUNC_ONE_TO_ONE
              scoped = func in one_to_one_scoped
            elif func in one_test:
              function = one_test[func]
              func_sig = FUNC_SELECTOR
            else:
              raise ValueError("unknown action")
          else:
            if func_sig is None:
              raise RuntimeError("function is set (%r) but func_sig is None" % (function,))
        # select URLs matching regexp
        # /regexp/
        elif action.startswith('/'):
          if action.endswith('/'):
            regexp = action[1:-1]
          else:
            regexp = action[1:]
          regexp = re.compile(regexp)
          function = lambda (P, U): regexp.search(U)
          function.__name__ = '/%s/' % (regexp,)
          func_sig = FUNC_SELECTOR
        # select URLs not matching regexp
        # -/regexp/
        elif action.startswith('-/'):
          if action.endswith('/'):
            regexp = action[2:-1]
          else:
            regexp = action[2:]
          regexp = re.compile(regexp)
          function = lambda (P, U): regexp.search(U)
          function.__name__ = '-/%s/' % (regexp,)
          func_sig = FUNC_SELECTOR
        # parent
        # ..
        elif action == '..':
          function = lambda (P, U): U.parent
          func_sig = FUNC_ONE_TO_ONE
        # select URLs ending in particular extensions
        elif action.startswith('.'):
          if action.endswith('/i'):
            exts, case = action[1:-2], False
          else:
            exts, case = action[1:], True
          exts = exts.split(',')
          function = lambda (P, U): has_exts( U, exts, case_sensitive=case )
          func_sig = FUNC_SELECTOR
        # select URLs not ending in particular extensions
        elif action.startswith('-.'):
          if action.endswith('/i'):
            exts, case = action[2:-2], False
          else:
            exts, case = action[2:], True
          exts = exts.split(',')
          function = lambda (P, U): not has_exts( U, exts, case_sensitive=case )
          func_sig = FUNC_SELECTOR
        else:
          raise ValueError("unknown function %r" % (func,))

    func0 = function
    # The pipeline itself passes (P, U) item tuples.
    #
    # All function accept a leading (P, U) tuple argument but emit only a U
    # result (or just a Booleans for selectors). Therefore, unless "scoped"
    # is true, we wrap each non-selector function to preserve the P component
    # in the output.
    #
    if scoped:
      def funcPU(item):
        return func0(item, *args, **kwargs)
    else:
      if func_sig == FUNC_SELECTOR:
        def funcPU(item):
          return func0(item, *args, **kwargs)
      elif func_sig == FUNC_ONE_TO_ONE:
        def funcPU(item):
          P, U = item
          return P, func0(item, *args, **kwargs)
      elif func_sig == FUNC_ONE_TO_MANY:
        def funcPU(item):
          P, U = item
          for i in func0(item, *args, **kwargs):
            yield P, i
      elif func_sig == FUNC_MANY_TO_MANY:
        # Many-to-many functions are different.
        # We split out the Ps and Us from the input items
        # and re-attach the P components by reverse mapping from the U results;
        # unrecognised Us get associated with Ps[0].
        #
        def funcPU(items):
          if not isinstance(items, list):
            items = list(items)
          if items:
            # preserve the first Pilfer context to attach to unknown items
            P0 = items[0][0]
            idmap = dict( [ ( id(item), item ) for item in items ] )
            Ps = [ item[0] for item in items ]
            Us = [ item[1] for item in items ]
          else:
            P0 = None
            idmap = {}
            Ps = []
            Us = []
          Us2 = func0(Ps, Us, *args, **kwargs)
          return [ (idmap.get(id(U), P0), U) for U in Us2 ]
      else:
        raise RuntimeError("unhandled func_sig %r" % (func_sig,))

    def trace_function(*a, **kw):
      ##D("DO %s(a=(%d args; %r),kw=%r)", action0, len(a), a, kw)
      ##D("   funcPU<%s:%d>=%r %r ...", funcPU.func_code.co_filename, funcPU.func_code.co_firstlineno, funcPU, dir(funcPU))
      with Pfx(action0):
        try:
          retval = funcPU(*a, **kw)
        except Exception as e:
          exception("TRACE: EXCEPTION: %s", e)
          raise
        ##D("DO %s: retval = %r", action0, retval)
        return retval

    return func_sig, trace_function

class PipeSpec(O):

  def __init__(self, name, argv):
    O.__init__(self)
    self.name = name
    self.argv = argv
    self.errors = []
    with Pfx(name):
      pipe_funcs, errors = argv_pipefuncs(argv)
      self.pipe_funcs = pipe_funcs
      self.errors.extend(errors)

class PilferRC(O):

  def __init__(self, filename):
    O.__init__(self)
    self.filename = filename
    self.print_flush = False
    self.pipe_specs = {}
    if filename is not None:
      self.loadrc(filename)

  def loadrc(self, fp):
    ''' Read a pilferrc file and load pipeline definitions.
    '''
    with Pfx(self.filename):
  cfg = ConfigParser()
      with open(self.filename) as fp:
  cfg.readfp(fp)
      dflts = cfg.defaults()
      for dflt, value in cfg.defaults().iteritems():
        if dflt == 'print_flush':
          self.print_flush = cfg.getboolean('DEFAULT', dflt)
        else:
          warning("unrecognised [DEFAULTS].%s: %s" % (dflt, value))
  for section in cfg.sections():
        with Pfx("[%s]", section):
    pipe_spec = cfg.get(section, 'pipe')
    debug("loadrc: %s: pipe = %s", section, pipe_spec)
          self.pipe_specs[section] = PipeSpec(section, shlex.split(pipe_spec))

  def __getitem__(self, pipename):
    ''' Fetch PipeSpec by name.
    '''
    return self.pipe_specs[pipename]

  def __setitem__(self, pipename, pipespec):
    specs = self.pipespecs
    if pipename in specs:
      raise KeyError("repeated definition of pipe named %r", pipename)
    specs[pipename] = pipespec

if __name__ == '__main__':
  import sys
  PilferRC('pilferrc')
  sys.exit(main(sys.argv))
