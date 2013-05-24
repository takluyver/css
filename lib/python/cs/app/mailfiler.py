#!/usr/bin/python
#
# Handler for rulesets similar to the format of cats2procmailrc(1cs).
#       - Cameron Simpson <cs@zip.com.au> 22may2011
#

from __future__ import print_function
from collections import namedtuple
from email import message_from_string
import email.parser
from email.utils import getaddresses
from getopt import getopt, GetoptError
import io
from logging import DEBUG
import mailbox
import os
import os.path
import re
import sys
from time import sleep
if sys.hexversion < 0x02060000: from sets import Set as set
import subprocess
from tempfile import TemporaryFile
from threading import Lock
import time
from cs.env import envsub
from cs.fileutils import abspath_from_file, file_property, files_property, Pathname
from cs.lex import get_white, get_nonwhite, get_qstr, unrfc2047
from cs.logutils import Pfx, setup_logging, debug, info, warning, error, D, LogTime
from cs.mailutils import Maildir, message_addresses, shortpath, ismaildir, make_maildir
from cs.obj import O, slist
from cs.threads import locked_property
from cs.app.maildb import MailDB
from cs.py3 import unicode as u, StringTypes

DEFAULT_MAILDIR_RULES = '$HOME/.mailfiler/{maildir.basename}'

def main(argv, stdin=None):
  if stdin is None:
    stdin = sys.stdin
  argv = list(argv)
  cmd = os.path.basename(argv.pop(0))
  setup_logging(cmd)
  usage = ( '''Usage: %s monitor [-1] [-d delay] [-n] [-N] [-R rules_pattern] maildirs...
  -1  File at most 1 message per Maildir.
  -d delay
      Delay between runs in seconds.
      Default is to make only one run over the Maildirs.
  -n  No remove. Keep filed messages in the origin Maildir.
  -R rules_pattern
      Specify the rules file pattern used to specify rules files from Maildir names.
      Default: %s'''
            % (cmd, DEFAULT_MAILDIR_RULES)
          )
  badopts = False

  if not argv:
    warning("missing op")
    badopts = True
  else:
    op = argv.pop(0)
    with Pfx(op):
      if op == 'monitor':
        justone = False
        delay = None
        no_remove = False
        no_save = False
        rules_pattern = DEFAULT_MAILDIR_RULES
        try:
          opts, argv = getopt(argv, '1d:nNR:')
        except GetoptError as e:
          warning("%s", e)
          badopts = True
        for opt, val in opts:
          with Pfx(opt):
            if opt == '-1':
              justone = True
            elif opt == '-d':
              try:
                delay = int(val)
              except ValueError as e:
                warning("%s: %s", e, val)
                badopts = True
              else:
                if delay <= 0:
                  warning("delay must be positive, got: %d", delay)
                  badopts = True
            elif opt == '-n':
              no_remove = True
            elif opt == '-N':
              no_save = True
            elif opt == '-R':
              rules_pattern = val
            else:
              warning("unimplemented option")
              badopts = True
        if not argv:
          warning("missing maildirs")
          badopts = True
        else:
          mdirpaths = [ Pathname(arg) for arg in argv ]
      else:
        warning("unrecognised op: %s", op)
        badopts = True

  if badopts:
    print(usage, file=sys.stderr)
    return 2

  with Pfx(op):
    if op == 'monitor':
      maildir_cache = {}
      filter_modes = FilterModes(justone=justone,
                                 delay=delay,
                                 no_remove=no_remove,
                                 no_save=no_save,
                                 maildb_path=os.environ['MAILDB'],
                                 maildir_cache={})
      maildirs = [ WatchedMaildir(mdirpath,
                                  filter_modes=filter_modes,
                                  rules_path=envsub(
                                               rules_pattern.format(maildir=mdirpath)))
                   for mdirpath in mdirpaths
                 ]
      try:
        while True:
          for MW in maildirs:
            debug("process %s", (MW.shortname,))
            with LogTime("%s.filter()", MW.shortname, threshold=1.0):
              for key, reports in MW.filter():
                pass
          if delay is None:
            break
          debug("sleep %ds", delay)
          sleep(delay)
        return 0
      except KeyboardInterrupt:
        for MW in maildirs:
          MW.close()
        return 1
    else:
      raise RunTimeError("unimplemented op")

def maildir_from_name(mdirname, maildir_root, maildir_cache):
    ''' Return the Maildir derived from mdirpath.
    '''
    mdirpath = resolve_mail_path(mdirname, maildir_root)
    if mdirpath not in maildir_cache:
      maildir_cache[mdirpath] = Maildir(mdirpath, create=True)
    return maildir_cache[mdirpath]

def resolve_mail_path(mdirpath, maildir_root):
  ''' Return the full path to the requested mail folder.
  '''
  if not os.path.isabs(mdirpath):
    if mdirpath.startswith('./') or mdirpath.startswith('../'):
      mdirpath = os.path.abspath(mdirpath)
    else:
      mdirpath = os.path.join(maildir_root, mdirpath)
  return mdirpath

class FilterModes(O):

  def __init__(self, **kw):
    self._O_omit = ('maildir_cache',)
    self._maildb_path = kw.pop('maildb_path')
    self._maildb_lock = Lock()
    O.__init__(self, **kw)

  @file_property
  def maildb(self, path):
    info("reload maildb %s", shortpath(path))
    return MailDB(path, readonly=True)

  def maildir(self, mdirname, environ=None):
    return maildir_from_name(mdirname, environ['MAILDIR'], self.maildir_cache)

class FilteringState(O):
  ''' Filtering state information used during rule evaluation.
      .message  Current message.
      .maildb   Current MailDB.
      .environ  Storage for variable settings.
  '''
 
  maildirs = {}

  def __init__(self, M, filter_modes, environ=None):
    ''' `M`:           The Message object to be filed.
        `filter_modes`: External state object, with maildb etc.
        `environ`:     Mapping which supplies initial variable names.
                       Default from os.environ.
    '''
    self.default_rule = None
    self.message = M
    self.filter_modes = filter_modes
    if environ is None:
      environ = os.environ
    self.environ = dict(environ)
    self.reuse_maildir = False
    self.used_maildirs = set()
    self._log = None

  @property
  def maildb(self):
    return self.filter_modes.maildb

  def maildir(self, mdirpath):
    return self.filter_modes.maildir(mdirpath, self.environ)

  def resolve(self, foldername):
    return resolve_mail_path(foldername, self.environ['MAILDIR'])

  @property
  def message(self):
    return self._message

  @message.setter
  def message(self, new_message):
    self._message = new_message
    self.message_path = None
    self.header_addresses = {}

  def log(self, *a):
    ''' Log a message.
    '''
    log = self._log
    if log is None:
      log = sys.stdout
    try:
      print(*[ unicode(s) for s in a], file=log)
    except UnicodeDecodeError as e:
      print("FilteringState.log: %s: a=%r" % (e, a), file=sys.stderr)

  def logto(self, logfilepath):
    ''' Direct log messages to the supplied `logfilepath`.
    '''
    if self._log:
      self._log.close()
      self._log = None
    try:
      self._log = io.open(logfilepath, "a", encoding='utf-8')
    except OSError as e:
      self.log("open(%s): %s" % (logfilepath, e))

  @property
  def groups(self):
    ''' The group mapping from the MailDB.
    '''
    return self.maildb.address_groups

  def ingroup(self, coreaddr, group_name):
    ''' Test if a core address is a member of the named group.
    '''
    group = self.groups.get(group_name)
    if group is None:
      warning("unknown group: %s", group_name)
      self.groups[group_name] = set()
      return False
    return coreaddr.lower() in group

  def addresses(self, *headers):
    ''' Return the core addresses from the current Message and supplied
        `headers`. Caches results for rapid rule evaluation.
    '''
    M = self.message
    if len(headers) != 1:
      addrs = set()
      for header in headers:
        addrs.update(self.addresses(header))
      return addrs
    header = headers[0]
    hamap = self.header_addresses
    if header not in hamap:
      hamap[header] = set( [ A for N, A in message_addresses(M, (header,)) ] )
    return hamap[header]

  def save_to_maildir(self, mdir, label, context, flags=''):
    ''' Save the current message to a Maildir unless we have already saved to
        this maildir.
    '''
    mdirpath = mdir.dir
    if mdirpath in self.used_maildirs:
      if not self.reuse_maildir:
        return None
    M = self.message
    if label and M.get('x-label', '') != label:
      # modifying message - make copy
      path = None
      M = message_from_string(M.as_string())
      M['X-Label'] = label
    else:
      path = self.message_path
    if path is None:
      savekey = mdir.save_message(M, flags=flags)
    else:
      savekey = mdir.save_filepath(path, flags=flags)
    savepath = mdir.keypath(savekey)
    if not path and not label:
      self.message_path = savepath
    self.log("    OK %s (%s)" % (shortpath(savepath), context))
    return savepath

  def save_to_mbox(self, mboxpath, label, context):
    M = self.message
    text = M.as_string(True)
    with open(mboxpath, "a") as mboxfp:
      mboxfp.write(text)

  def pipe_message(self, argv, mfp=None, context=None):
    ''' Pipe a message to the command specific by `argv`.
        `mfp` is a file containing the message text.
        If `mfp` is None, use the text of the current message.
    '''
    if mfp is None:
      message_path = self.message_path
      if message_path:
        with open(message_path) as mfp:
          return self.pipe_message(argv, mfp=mfp, context=context)
      else:
        with TemporaryFile('w+') as mfp:
          mfp.write(str(self.message))
          mfp.flush()
          mfp.seek(0)
          return self.pipe_message(argv, mfp=mfp, context=context)
    retcode = subprocess.call(argv, env=self.environ, stdin=mfp)
    self.log("    %s => | %s (%s)" % (("OK" if retcode == 0 else "FAIL"), argv, context))
    return retcode == 0

  def sendmail(self, address, mfp=None, context=None):
    ''' Dispatch a message to `address`.
        `mfp` is a file containing the message text.
        If `mfp` is None, use the text of the current message.
    '''
    return self.pipe_message([self.environ.get('SENDMAIL', 'sendmail'), '-oi', address], mfp=mfp, context=context)

re_UNQWORD = re.compile(r'[^,\s]+')

# header[,header,...]:
re_HEADERLIST = re.compile(r'([a-z][\-a-z0-9]*(,[a-z][\-a-z0-9]*)*):', re.I)

# identifier=
re_ASSIGN = re.compile(r'([a-z]\w+)=', re.I)

# group membership test: (A|B|C|...)
re_INGROUP = re.compile(r'\(\s*[a-z]\w+(\s*\|\s*[a-z]\w+)*\s*\)', re.I)

# header[,header,...].func(
re_HEADERFUNCTION = re.compile(r'([a-z][\-a-z0-9]*(,[a-z][\-a-z0-9]*)*)\.([a-z][_a-z0-9]*)\(', re.I)

def parserules(fp):
  ''' Read rules from `fp`, yield Rules.
  '''
  if isinstance(fp, StringTypes):
    with open(fp) as rfp:
      for R in parserules(rfp):
        yield R
    return

  filename = getattr(fp, 'name', None)
  if filename is None:
    file_label = str(type(fp))
  else:
    file_label = shortpath(filename)
  info("PARSE RULES: %s", file_label)
  lineno = 0
  R = None
  for line in fp:
    lineno += 1
    with Pfx("%s:%d", file_label, lineno):
      if filename:
        if not line.endswith('\n'):
          raise ValueError("short line at EOF")

      # skip comments
      if line.startswith('#'):
        continue

      # remove newline and trailing whitespace
      line = line.rstrip()

      # skip blank lines
      if not line:
        continue

      _, offset = get_white(line, 0)
      if not _:
        # new rule
        # yield old rule if in progress
        if R:
          yield R
        R = None

        if line[offset] == '<':
          # include another categories file
          _, offset = get_white(line, offset+1)
          subfilename, offset = get_nonwhite(line, offset=offset)
          if not subfilename:
            raise ValueError("missing filename")
          subfilename = envsub(subfilename)
          if filename:
            subfilename = abspath_from_file(subfilename, filename)
          else:
            subfilename = os.path.abspath(subfilename)
          for R in parserules(subfilename):
            yield R
          continue

        # new rule
        R = Rule(filename=(filename if filename else file_label), lineno=lineno)

        m = re_ASSIGN.match(line, offset)
        if m:
          R.actions.append( ('ASSIGN', (m.group(1), line[m.end():])) )
          yield R
          R = None
          continue

        while True:
          if line[offset] == '+':
            R.flags.halt = False
            offset += 1
          elif line[offset] == '=':
            R.flags.halt = True
            offset += 1
          if line[offset] == '!':
            R.flags.alert += 1
            offset += 1
          else:
            break

        targets, offset = get_targets(line, offset)
        for target in targets:
          R.actions.append( ('TARGET', target) )

        # gather label
        _, offset = get_white(line, offset)
        if not _ or offset == len(line):
          R.label = ''
          warning("no label or condition")
          continue
        if line[offset] == '"':
          label, offset = get_qstr(line, offset)
        else:
          label, offset = get_nonwhite(line, offset)
        if label == '.':
          label = ''
        R.label = label
        _, offset = get_white(line, offset)

      # condition
      condition_flags = O(invert=False)

      if not _ or offset == len(line):
        warning('no condition')
        continue

      if line[offset] == '!':
        condition_flags.invert = True
        _, offset = get_white(line, offset+1)
        if offset == len(line):
          warning('no condition after "!"')
          continue

      # . always matches - don't bother storing it as a test
      if line[offset:] == '.':
        continue

      # leading hdr1,hdr2.func(
      m = re_HEADERFUNCTION.match(line, offset)
      if m:
        header_names = tuple( H.lower() for H in m.group(1).split(',') if H )
        testfuncname = m.group(3)
        offset = m.end()
        _, offset = get_white(line, offset)
        if offset == len(line):
          raise ValueError("missing argument to header function")
        if line[offset] == '"':
          test_string, offset = get_qstr(line, offset)
          _, offset = get_white(line, offset)
          if offset == len(line) or line[offset] != ')':
            raise ValueError("missing closing parenthesis after header function argument")
          offset += 1
          _, offset = get_white(line, offset)
          if offset < len(line):
            raise ValueError("extra text after header function: %r" % (line[offset:],))
        else:
          raise ValueError("unexpected argument to header function, expected double quoted string")
        C = Condition_HeaderFunction(condition_flags, header_names, testfuncname, test_string)
      else:
        # leading hdr1,hdr2,...:
        m = re_HEADERLIST.match(line, offset)
        if m:
          header_names = tuple( H.lower() for H in m.group(1).split(',') if H )
          offset = m.end()
          if offset == len(line):
            raise ValueError("missing match after header names")
        else:
          header_names = ('to', 'cc', 'bcc')
        # headers:/regexp
        if line[offset] == '/':
          regexp = line[offset+1:]
          if regexp.startswith('^'):
            atstart = True
            regexp = regexp[1:]
          else:
            atstart = False
          C = Condition_Regexp(condition_flags, header_names, atstart, regexp)
        else:
          # headers:(group[|group...])
          m = re_INGROUP.match(line, offset)
          if m:
            group_names = set( w.strip().lower() for w in m.group()[1:-1].split('|') )
            offset = m.end()
            if offset < len(line):
              raise ValueError("extra text after groups: %s" % (line,))
            C = Condition_InGroups(condition_flags, header_names, group_names)
          else:
            if line[offset] == '(':
              raise ValueError("incomplete group match at: %s" % (line[offset:]))
            # just a comma separated list of addresses
            # TODO: should be RFC2822 list instead?
            addrkeys = [ coreaddr for realname, coreaddr in getaddresses( (line[offset:],) ) ]
            C = Condition_AddressMatch(condition_flags, header_names, addrkeys)

      R.conditions.append(C)

  if R is not None:
    yield R

def get_targets(s, offset):
  ''' Parse list of targets from the string `s` strarting at `offset`.
      Return the list of targets strings and the new offset.
  '''
  targets = []
  while offset < len(s) and not s[offset].isspace():
    if s[offset] == '"':
      target, offset = get_qstr(s, offset)
    else:
      m = re_UNQWORD.match(s, offset)
      if m:
        target = m.group()
        offset = m.end()
      else:
        error("parse failure at %d: %s", offset, s)
        raise ValueError("syntax error")
    targets.append(target)
    if offset < len(s) and s[offset] == ',':
      offset += 1
  return targets, offset

class _Condition(O):
  
  def __init__(self, flags, header_names):
    self.flags = flags
    self.header_names = header_names

  def match(self, fstate):
    status = False
    M = fstate.message
    for header_name in self.header_names:
      for header_value in M.get_all(header_name, ()):
        if self.test_value(fstate, header_name, header_value):
          status = True
          break
    if self.flags.invert:
      status = not status
    return status

class Condition_Regexp(_Condition):

  def __init__(self, flags, header_names, atstart, regexp):
    _Condition.__init__(self, flags, header_names)
    self.atstart = atstart
    self.regexp = re.compile(regexp)
    self.regexptxt = regexp

  def test_value(self, fstate, header_name, header_value):
    if self.atstart:
      return self.regexp.match(header_value)
    return self.regexp.search(header_value)

class Condition_AddressMatch(_Condition):

  def __init__(self, flags, header_names, addrkeys):
    _Condition.__init__(self, flags, header_names)
    self.addrkeys = tuple( k for k in addrkeys if len(k) > 0 )

  def test_value(self, fstate, header_name, header_value):
    for address in fstate.addresses(header_name):
      for key in self.addrkeys:
        if key.startswith('{{') and key.endswith('}}'):
          warning("OBSOLETE address key: %s", key)
          group_name = key[2:-2].lower()
          if fstate.ingroup(address, group_name):
            return True
        elif address.lower() == key.lower():
          return True
    return False

class Condition_InGroups(_Condition):

  def __init__(self, flags, header_names, group_names):
    _Condition.__init__(self, flags, header_names)
    self.group_names = group_names

  def test_value(self, fstate, header_name, header_value):
    for address in fstate.addresses(header_name):
      for group_name in self.group_names:
        if fstate.ingroup(address, group_name):
          debug("match %s to (%s)", address, group_name)
          return True
    return False

class Condition_HeaderFunction(_Condition):

  def __init__(self, flags, header_names, funcname, test_string):
    _Condition.__init__(self, flags, header_names)
    self.funcname = funcname
    self.test_string = test_string
    test_method = 'test_func_' + funcname
    try:
      self.test_func = getattr(self, test_method)
    except AttributeError:
      raise ValueError("invalid header function .%s()" % (funcname,))

  def test_value(self, fstate, header_name, header_value):
    return self.test_func(fstate, header_name, header_value)

  def test_func_contains(self, fstate, header_name, header_value):
    return self.test_string in header_value

_FilterReport = namedtuple('FilterReport',
                          'rule matched saved_to ok_actions failed_actions')
def FilterReport(rule, matched, saved_to, ok_actions, failed_actions):
  if not matched:
    if saved_to:
      raise RunTimeError("matched(%r) and not saved_to(%r)" % (matched, saved_to))
  return _FilterReport(rule, matched, saved_to, ok_actions, failed_actions)

class Rule(O):

  def __init__(self, filename, lineno):
    self.filename = filename
    self.lineno = lineno
    self.conditions = slist()
    self.actions = slist()
    self.flags = O(alert=0, halt=False)
    self.label = ''

  def match(self, fstate):
    # all conditions must match
    for C in self.conditions:
      if not C.match(fstate):
        return False
    return True

  def filter(self, fstate):
    M = fstate.message
    context=("%s:%d" % (shortpath(self.filename), self.lineno))
    with Pfx(context):
      saved_to = []
      ok_actions = []
      failed_actions = []
      matched = self.match(fstate)
      if matched:
        for action, arg in self.actions:
          ok = False
          try:
            if action == 'TARGET':
              target = envsub(arg, fstate.environ)
              if target.startswith('|'):
                shcmd = target[1:]
                if fstate.pipe_message(['/bin/sh', '-c', shcmd], context=context):
                  ok = True
                  saved_to.append(target)
                else:
                  error("failed to pipe to %s", target)
                  failed_actions.append( (action, arg, "pipe "+target) )
              elif '@' in target:
                if fstate.sendmail(target, context=context):
                  ok = True
                  saved_to.append(target)
                else:
                  error("failed to sendmail to %s", target)
                  failed_actions.append( (action, arg, "sendmail "+target) )
              else:
                mailpath = fstate.resolve(target)
                if not os.path.exists(mailpath):
                  make_maildir(mailpath)
                if ismaildir(mailpath):
                  mdir = fstate.maildir(target)
                  if self.flags.alert:
                    maildir_flags = 'F'
                  else:
                    maildir_flags = ''
                  savepath = fstate.save_to_maildir(mdir,
                                                       self.label,
                                                       context=context,
                                                       flags=maildir_flags)
                  ok = True
                  # we get None if the message has already been saved here
                  if savepath is not None:
                    saved_to.append(savepath)
                  else:
                    debug("repeated filing to maildir, skipping %s", mdir)
                else:
                  fstate.save_to_mbox(mailpath, self.label, context=context)
            elif action == 'ASSIGN':
              envvar, s = arg
              value = fstate.environ[envvar] = envsub(s, fstate.environ)
              ok = True
              debug("ASSIGN %s=%s", envvar, value)
              if envvar == 'LOGFILE':
                fstate.logto(value)
              elif envvar == 'DEFAULT':
                R = fstate.default_rule = Rule(self.filename, self.lineno)
                R.actions.append( ('TARGET', '$DEFAULT') )
            else:
              raise RuntimeError("unimplemented action \"%s\"" % action)
          except (AttributeError, NameError):
            raise
          except Exception as e:
            warning("EXCEPTION %r", e)
            failed_actions.append( (action, arg, e) )
            raise
          else:
            if ok:
              ok_actions.append( (action, arg) )
        if not saved_to:
          debug("MATCHED but no saved_to: %s", self)
        else:
          debug("MATCHED and saved_to=%s: %s", saved_to, self)
      return FilterReport(self, matched, saved_to, ok_actions, failed_actions)

class Rules(list):
  ''' Simple subclass of list storing rules, with methods to load
      rules and filter a message using the rules.
  '''

  def __init__(self, rules_file=None):
    list.__init__(self)
    self.vars = {}
    self.rule_files = set()
    if rules_file is not None:
      self.load(rules_file)

  def load(self, fp):
    ''' Import an open rule file.
    '''
    new_rules = list(parserules(fp))
    self.rule_files.update( R.filename for R in new_rules )
    self.extend(new_rules)

  def filter(self, fstate):
    ''' Filter the current message according to the rules.
        Yield FilterReports for each rule consulted.
        If no rule matches and $DEFAULT is set, yield a FilterReport for
        filing to $DEFAULT, with .rule set to None.
    '''
    done = False
    saved_to = []
    for R in self:
      report = R.filter(fstate)
      M = fstate.message
      if report.matched:
        if not report.saved_to:
          debug("matched but not saved_to: %s", R)
        saved_to.extend(report.saved_to)
        if R.flags.alert:
          self.alert("%s: %s" % (M.get('from', '').strip(), M.get('subject', '').strip()))
      else:
        if report.saved_to:
          error("matched is False, but saved_to = %s", saved_to)
      yield report
      if report.matched:
        if R.flags.halt:
          done = True
          break
    if not done:
      if not saved_to:
        R = fstate.default_rule
        if not R:
          warning("NO DEFAULT: message not saved and no $DEFAULT")
        else:
          report = R.filter(fstate)
          if not report.matched:
            raise RunTimeError("default rule faled to match! %r", R)
          saved_to.extend(report.saved_to)
          if not saved_to:
            warning("message not saved by any rules")
          yield report
      else:
        debug("%d filings, skipping $DEFAULT", len(saved_to))

  def alert(self, alert_message):
    xit = subprocess.call(['alert', 'MAILFILER: ' + alert_message])
    if xit != 0:
      warning("non-zero exit from alert: %d", xit)
    return xit

class WatchedMaildir(O):
  ''' A class to monitor a Maildir and filter messages.
  '''

  def __init__(self, mdir, filter_modes, rules_path=None):
    self.mdir = Maildir(resolve_mail_path(mdir, os.environ['MAILDIR']))
    self.filter_modes = filter_modes
    if rules_path is None:
      rules_path = os.path.join(self.mdir.dir, '.rules')
    self._rules_paths = [ rules_path ]
    self._rules_lock = Lock()
    self.lurking = set()
    self.flush()
    warning("%d rules", len(self.rules))

  def __str__(self):
    return "<WatchedMaildir %s modes=%s, %d rules, %d lurking>" \
           % (self.shortname,
              self.filter_modes,
              len(self.rules),
              len(self.lurking))

  @property
  def shortname(self):
    return self.mdir.shortname

  def flush(self):
    ''' Forget state.
        The set of lurkers is emptied.
    '''
    self.lurking = set()

  def close(self):
    self.filter_modes.maildb.close()

  @files_property
  def rules(self, rules_paths):
    # base file is at index 0
    path0 = rules_paths[0]
    R = Rules(path0)
    # produce rules file list with base file at index 0
    paths = [ path0 ] + [ path for path in R.rule_files if path != path0 ]
    return paths, R

  def filter(self):
    ''' Scan this spool Maildir for messages to filter.
        Yield (key, FilterReports) for all messages filed.
	Update the set of lurkers with any keys not removed to prevent
	filtering on subsequent calls.
    '''
    with Pfx("%s: filter", self.shortname):
      self.mdir.flush()
      nmsgs = 0
      skipped = 0
      with LogTime("all keys") as all_keys_time:
        mdir = self.mdir
        for key in mdir.keys():
          if key in self.lurking:
            debug("skip processed key: %s", key)
            skipped += 1
            continue
          nmsgs += 1
          with LogTime("key = %s", key, threshold=1.0, level=DEBUG):
            M = mdir[key]
            fstate = FilteringState(M, self.filter_modes)
            fstate.message_path = mdir.keypath(key)
            fstate.logto(envsub("$HOME/var/log/mailfiler"))
            fstate.log( (u("%s %s") % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                       unrfc2047(M.get('subject', '_no_subject'))))
                       .replace('\n', ' ') )
            fstate.log("  " + unrfc2047(M.get('from', '_no_from')))
            fstate.log("  " + M.get('message-id', '<?>'))
            fstate.log("  " + shortpath(mdir.keypath(key)))
            saved_to = []
            reports = []
            for report in self.rules.filter(fstate):
              if report.matched:
                reports.append(report)
                saved_to.extend(report.saved_to)
              else:
                if report.saved_to:
                  error("UNMATCHED RULE: saved_to=%s", report.saved_to)
            if not saved_to:
              fstate.log("ERROR: message not saved, lurking key %s", key)
              error("message not saved, lurking key %s", key)
              self.lurking.add(key)
            elif self.filter_modes.no_remove:
              fstate.log("no_remove: message not removed, lurking key %s", key)
              self.lurking.add(key)
            else:
              debug("remove message key %s", key)
              mdir.remove(key)
              self.lurking.discard(key)
            yield key, reports
          if self.filter_modes.justone:
            break
      if nmsgs or all_keys_time.elapsed >= 0.2:
        info("filtered %d messages (%d skipped) in %5.3fs", nmsgs, skipped, all_keys_time.elapsed)

if __name__ == '__main__':
  sys.exit(main(sys.argv))
