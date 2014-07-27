#!/usr/bin/python

from __future__ import print_function
import os
from os import geteuid, getegid
import stat
from collections import namedtuple
from pwd import getpwuid, getpwnam
from grp import getgrgid, getgrnam
from stat import S_ISUID, S_ISGID
from threading import RLock
from cs.logutils import error, warning, X
from cs.threads import locked

DEFAULT_DIR_ACL = 'o:rwx-'
DEFAULT_FILE_ACL = 'o:rw-x'

NOBODY = -1
NOGROUP = -1

user_map = {}
group_map = {}

def username(uid):
  ''' Look up the login name associated with the supplied `uid`.
      Return None if unknown. Caches results, including lookup failure.
  '''
  global user_map
  try:
    user = user_map[uid]
  except KeyError:
    try:
      user = getpwuid(uid)
    except KeyError:
      user = None
    else:
      user = user.pw_name
    user_map[uid] = user
    if user not in user_map:
      user_map[user] = uid
  return user

def userid(username):
  ''' Look up the user id associated with the supplied `username`.
      Return None if unknown. Caches results, including lookup failure.
  '''
  global user_map
  try:
    uid = user_map[username]
  except KeyError:
    try:
      uid = getpwnam(username)
    except KeyError:
      uid = None
    else:
      uid = user.pw_uid
    user_map[username] = uid
    if uid not in user_map:
      user_map[uid] = username
  return uid

def groupname(gid):
  ''' Look up the group name associated with the supplied `gid`.
      Return None if unknown. Caches results, including lookup failure.
  '''
  global group_map
  try:
    group = group_map[gid]
  except KeyError:
    try:
      group = getpwuid(gid)
    except KeyError:
      group = None
    else:
      group = group.pw_name
    group_map[gid] = group
    if group not in group_map:
      group_map[group] = gid
  return group

def groupid(groupname):
  ''' Look up the group id associated with the supplied `groupname`.
      Return None if unknown. Caches results, including lookup failure.
  '''
  global group_map
  try:
    gid = group_map[groupname]
  except KeyError:
    try:
      gid = getpwnam(groupname)
    except KeyError:
      gid = None
    else:
      gid = group.pw_uid
    group_map[groupname] = gid
    if gid not in group_map:
      group_map[gid] = group
  return gid

Stat = namedtuple('Stat', 'st_mode st_ino st_dev st_nlink st_uid st_gid st_size st_atime st_mtime st_ctime')

def permbits_to_allow_deny(bits):
  ''' Take a UNIX 3-bit permission value and return the ACL allow and deny strings.
      Example: 6 (110) => 'rw', 'x'
  '''
  add = ''
  sub = ''
  for c, bit in ('r', 0x04), ('w', 0x02), ('x', 0x01):
    if bits&bit:
      add += c
    else:
      sub += c
  return add, sub

class AC(object):
  __slots__ = ('prefix', 'allow', 'deny')

  def __init__(self, prefix, allow, deny):
    ''' Initialise AC with `allow` and `deny` permission strings.
    '''
    self.prefix = prefix
    self.allow = allow
    self.deny = deny

  def __repr__(self):
    return ':'.join( (self.__class__.__name__, self.textencode()) )

  def textencode(self):
    return self.prefix + ':' + self.allow + '-' + self.deny

  def __call__(self, M, accesses):
    ''' Call the AC with the Meta `M` and the required permissions `accesses`.
        Returns False if any access is in .deny, otherwise returns True if
        all accesses are in .allow.
        Subclasses override this method and check the AC for
        applicability before deferring to this method to test access;
        they return None if the AC is not applicable, for example
        an AC_Owner when the Meta has no owner or the Meta owner
        does not match the caller owner.
    '''
    allow = self.allow
    deny = self.deny
    for access in accesses:
      if access in deny:
        return False
      if access not in allow:
        return False
    return True

  @property
  def unixmode(self):
    ''' Return the 3-buit UNIX mode for this access.
    '''
    mode = 0
    for a in self.allow:
      if a == 'r':
        mode |= 4
      elif a == 'w':
        mode |= 2
      elif a == 'x':
        mode |= 1
      else:
        warning("AC.unixmode: ignoring unsupported permission %r", a)
    X("unixmode(%r) ==> %o", self.textencode(), mode)
    return mode

class AC_Owner(AC):
  def __init__(self, allow, deny):
    AC.__init__(self, 'o', allow, deny)
  def __call__(self, M, accesses, owner):
    Mowner = M.get('u')
    if Mowner is None or Mowner != owner:
      return None
    return AC.__call__(self, M, accesses)

class AC_Group(AC):
  def __init__(self, allow, deny):
    AC.__init__(self, 'g', allow, deny)
  def __call__(self, M, accesses, group):
    Mgroup = M.get('g')
    if Mgroup is None or Mgroup != group:
      return None
    return AC.__call__(self, M, accesses)

class AC_Other(AC):
  def __init__(self, allow, deny):
    AC.__init__(self, '*', allow, deny)

_AC_prefix_map = {
  'o':  AC_Owner,
  'g':  AC_Group,
  '*':  AC_Other,
}

def decodeAC(ac_text):
  ''' Factory function to return a new AC from an encoded AC.
  '''
  prefix, allow_deny = ac_text.split(':', 1)
  allow, deny = allow_deny.split('-', 1)
  try:
    ac_class = _AC_prefix_map[prefix]
  except KeyError:
    raise ValueError("invalid prefix %r from %r" % (prefix, ac_text))
  return ac_class(allow, deny)

def decodeACL(acl_text):
  ''' Return a list of ACs from the encoded list `acl_text`.
  '''
  acl = []
  for ac_text in acl_text.split(','):
    if ac_text:
      try:
        ac = decodeAC(ac_text)
      except ValueError as e:
        error("invalid ACL element ignored: %r: %s", ac_text, e)
      else:
        acl.append(ac)
  return acl

def encodeACL(acl):
  ''' Encode a list of AC instances as text.
  '''
  return ','.join( [ ac.textencode() for ac in acl ] )

class Meta(dict):
  ''' Inode metadata: times, permissions, ownership etc.

      This is a dictionary with the following keys:

      'u': owner
      'g': group owner
      'a': ACL
      'm': modification time, a float
      'su': setuid
      'sg': setgid
  '''
  def __init__(self, E):
    dict.__init__(self)
    self.E = E
    self._acl = None
    self._lock = RLock()

  def textencode(self):
    ''' Encode the metadata in text form.
    '''
    # update 'a' if necessary
    _acl = self._acl
    if _acl is not None:
      self['a'] = encodeACL(_acl)
    return ";".join("%s:%s" % (k, self[k]) for k in sorted(self.keys()))

  def encode(self):
    ''' Encode the metadata in binary form: just text transcribed in UTF-8.
    '''
    return self.textencode().encode()

  @property
  def user(self):
    ''' Return the username associated with this Meta's owner.
    '''
    u = self.get('u')
    if u is not None and not isinstance(u, str):
      u = username(u)
    return u

  @user.setter
  def user(self, u):
    ''' Set the owner (user) of this Meta.
    '''
    self['u'] = u

  @user.deleter
  def user(self):
    ''' Remove the owner of this Meta.
    '''
    if 'u' in self:
      del self['u']

  @property
  def uid(self):
    ''' Return the user id associated with this Meta's owner.
    '''
    u = self.get('u')
    if u is not None and not isinstance(u, int):
      u = userid(u)
    return u

  @uid.setter
  def uid(self, u):
    ''' Set the owner (user) of this Meta.
        Saves the user name of the supplied uid, or the uid if the
        user name cannot be looked up.
    '''
    user = username(u)
    if user is not None:
      u = user
    self['u'] = u

  @uid.deleter
  def uid(self):
    ''' Remove the owner (user) of this Meta.
    '''
    if 'u' in self:
      del self['u']

  @property
  def group(self):
    ''' Return the groupname associated with this Meta's group owner.
    '''
    g = self.get('g')
    if g is not None and not isinstance(g, str):
      g = username(g)
    return g

  @group.setter
  def group(self, g):
    ''' Set the group owner of this Meta.
    '''
    self['g'] = g

  @group.deleter
  def group(self):
    ''' Remove the group owner of this Meta.
    '''
    if 'g' in self:
      del self['g']

  @property
  def gid(self):
    ''' Return the group id associated with this Meta's group owner.
    '''
    g = self.get('g')
    if g is not None and not isinstance(g, int):
      g = groupid(g)
    return g

  @gid.setter
  def gid(self, g):
    ''' Set the owner (user) of this Meta.
        Saves the group name of the supplied gid, or the gid if the
        group name cannot be looked up.
    '''
    group = groupname(g)
    if group is not None:
      g = group
    self['g'] = g

  @gid.deleter
  def gid(self):
    ''' Remove the owner (user) of this Meta.
    '''
    if 'g' in self:
      del self['g']

  @property
  def mtime(self):
    return self.get('m', 0.0)

  @mtime.setter
  def mtime(self, when):
    self['m'] = when

  @property
  @locked
  def acl(self):
    ''' Return the live list of AC instances, decoded at need.
    '''
    _acl = self._acl
    if _acl is None:
      X("META.acl: self = %r", self)
      dflt_acl = DEFAULT_DIR_ACL if self.E.isdir else DEFAULT_FILE_ACL
      acl_text = self.get('a', dflt_acl)
      X("META.acl_text: %r", acl_text)
      _acl = self._acl = decodeACL(acl_text)
      X("META.acl => %r", _acl)
    return _acl

  @acl.setter
  @locked
  def acl(self, ac_L):
    ''' Rewrite the ACL with a list of AC instances.
    '''
    self['a'] = encodeACL(ac_L)
    self._acl = None
    X("META: set ACL to %s", self['a'])

  @property
  def setuid(self):
    ''' Return whether this Meta is setuid.
    '''
    return self.get('su', False)

  @setuid.setter
  def setuid(self, flag):
    ''' Set the setuidness of this Meta.
    '''
    if flag:
      self['su'] = flag
    elif 'su' in self:
      del self['su']

  @property
  def setgid(self):
    ''' Return whether this Meta is setgid.
    '''
    return self.get('sg', False)

  @setuid.setter
  def setgid(self, flag):
    ''' Set the setgidness of this Meta.
    '''
    if flag:
      self['sg'] = flag
    elif 'sg' in self:
      del self['sg']

  def chmod(self, mode):
    ''' Apply UNIX permissions to ACL.
    '''
    if mode&S_ISUID:
      self.setuid = True
    else:
      self.setuid = False
    if mode&S_ISGID:
      self.setgid = True
    else:
      self.setgid = False
    self.acl = [ AC_Owner( *permbits_to_allow_deny( (mode>>6)&7 ) ),
                 AC_Group( *permbits_to_allow_deny( (mode>>3)&7 ) ),
                 AC_Other( *permbits_to_allow_deny( mode&7 ) )
               ] + [ ac for ac in self.acl if ac.prefix in ('o', 'g', '*') ]
    X("META.chmod: meta=%s", self.textencode())

  def update(self, metatext):
    ''' Update the Meta fields from the supplied metatext.
    '''
    for metafield in metatext.split(';'):
      metafield = metafield.strip()
      if not metafield:
        continue
      try:
        k, v = metafield.split(':', 1)
      except ValueError:
        error("ignoring bad metatext field: %r", metafield)
        continue
      self[k] = v

  def update_from_stat(self, st):
    ''' Apply the contents of a stat object to this Meta.
    '''
    self.mtime = float(st.st_mtime)
    self.uid = st.st_uid
    self.gid = st.st_gid
    self.chmod(st.st_mode & 0o777)
    # TODO: setuid, setgid, sticky

  @property
  def unix_perms(self):
    ''' Return (user, group, unix-mode-bits).
        The user and group are strings, not uid/gid.
        For ACLs with more than one user or group this is only an approximation,
        keeping the permissions for the frontmost user and group.
    '''
    if self.E.isdir:
      X("meta.unix_perms: %s: set S_IFDIR", self.E.name)
      perms = stat.S_IFDIR
    elif self.E.isfile:
      X("meta.unix_perms: %s: set S_IFREG", self.E.name)
      perms = stat.S_IFREG
    else:
      warning("Meta.unix_perms: neither a dir nor a file")
    for ac in self.acl:
      if ac.prefix == 'o':
        perms |= ac.unixmode << 6
      elif ac.prefix == 'g':
        perms |= ac.unixmode << 3
      elif ac.prefix == '*':
        perms |= ac.unixmode
      else:
        warning("Meta.unix_perms: ignoring ACL element %s", ac.extencode)
    # TODO: setuid, setgid, sticky
    if self.setuid:
      perms |= S_ISUID
    if self.setgid:
      perms |= S_ISUID
    uid = self.uid
    if uid is None:
      uid = NOBODY
    gid = self.gid
    if gid is None:
      gid = NOGROUP
    X("META.unix_perms: %d, %d, %o", uid, gid, perms)
    return uid, gid, perms

  def access(self, amode, user=None, group=None):
    ''' POSIX like access call, accepting os.access `amode`.
    '''
    X("Meta.access: return TRUE ALWAYS")
    return True
    u, g, perms = self.unix_perms
    if amode & os.R_OK:
      if user is not None and user == u:
        if not ( (perms>>6) & 4 ):
          X("Meta.access: FALSE")
          return False
      elif group is not None and group == g:
        if not ( (perms>>3) & 4 ):
          X("Meta.access: FALSE")
          return False
      elif not ( perms & 4 ):
          X("Meta.access: FALSE")
          return False
    if amode & os.W_OK:
      if user is not None and user == u:
        if not ( (perms>>6) & 2 ):
          X("Meta.access: FALSE")
          return False
      elif group is not None and group == g:
        if not ( (perms>>3) & 2 ):
          X("Meta.access: FALSE")
          return False
      elif not ( perms & 2 ):
          X("Meta.access: FALSE")
          return False
    if amode & os.X_OK:
      if user is not None and user == u:
        if not ( (perms>>6) & 1 ):
          X("Meta.access: FALSE")
          return False
      elif group is not None and group == g:
        if not ( (perms>>3) & 1 ):
          X("Meta.access: FALSE")
          return False
      elif not ( perms & 1 ):
          X("Meta.access: FALSE")
          return False
    X("Meta.access: TRUE")
    return True

  def stat(self):
    ''' Return a stat object computed from this Meta data.
    '''
    st_uid, st_gid, st_mode = self.unix_perms
    st_ino = -1
    st_dev = -1
    st_nlink = 1
    st_size = self.E.size()
    st_atime = 0
    st_mtime = self.mtime
    st_ctime = 0
    return Stat(st_mode, st_ino, st_dev, st_nlink, st_uid, st_gid, st_size, st_atime, st_mtime, st_ctime)
