#!/usr/bin/env python3
"""
Github Repo Mirror Tool

This mirrors pull requests, with special handling for maintaining a fork
of an "awesome" repo.

Setup:

1. `pip install ghapi`
2. Create token in Developer settings > Personal access tokens
3. Put token in ~/.private/github-token

List a page of PRs:

    ./bin/github-mirror.py -s 1

Mirror PR number 10:

    ./bin/github-mirror.py -m 10

For more help use `-h` flag and comments in this file.
"""

import yaml, json, sys, re, os, logging, shutil
import sqlite3
import requests

from collections import defaultdict
from dataclasses import dataclass, field, fields, asdict
from time import sleep
from pprint import pprint
from subprocess import run, PIPE, STDOUT
from pathlib import Path
from functools import wraps
from textwrap import indent
from datetime import datetime
from shlex import quote

from ghapi.all import GhApi, paged
from urllib.error import HTTPError

###############################################################
# Classes

@dataclass
class Repo:
    """Github Repo."""
    owner: str
    repo: str

    @property
    def gh(self):
        return asdict(self)

    def __str__(self):
        return f'{self.owner}/{self.repo}'

@dataclass
class Pull:
    """Github Pull Request Reference."""
    remote: str
    num: int

    @property
    def gh(self):
        return dict(
            pull_number=self.num,
            issue_number=self.num,
            owner=self.repo.owner,
            repo=self.repo.repo,
        )

    @property
    def repo(self):
        return REMOTES[self.remote]
    
    def __str__(self):
        return f'{self.repo}#{self.num}'

    @property
    def key(self):
        return f'pull/{self.repo}/{self.num}'

class Record:
    """Generic DB Record."""
    key: str = ''  # normalized url
    ver: int = 0

    def __init__(self, **kw):
        self.__dict__ = kw

    def __setitem__(self, k, v):
        setattr(self, k, v)
    
    def __getitem__(self, k):
        return getattr(self, k)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def keys(self):
        return self.__dict__.keys()

    def __str__(self):
        return 'Record(%s)' % self.__dict__

@dataclass
class ListInfo:
    """Awesome List Metadata."""
    url: str = ''
    key: str = ''  # normalized url
    title: str = ''
    topic: str = ''
    desc: str = ''
    stars: int = 0
    updated: str = ''
    size: int = 0 
    status: str = ''
    redir: str = ''
    code: int = 0
    error: str = ''
    ver: int = 0
    owner: str = ''  # ignored - old
    head: str = ''  # ignored - old
    links: list = None  # ignored - old

    # not stored, only for sort+fixup
    indent = ''
    alts = None

    def build_link(self, prefix=None):
        title = self.title
        if prefix:
            title = ': '.join([*prefix, title])

        link = f'{self.indent}- [{title}]({self.url})'
        if self.desc:
            link += f' - {self.desc}'

        return link

    def __str__(self):
        out = []
        for f in 'topic desc url stars size redir error status'.split():
            if v := getattr(self, f, None):
                out.append((f, v))
       
        if self.code not in [0, 200]:
            out.append(('code', self.code))

        if self.updated:
            out.append(('mtime', _format_time(self.updated)))

        if m := re.match(r'https?://github.com/([^/#]+)/([^/#]+)', self.url):
            head = f'{self.title} from @{m[1]}'
        else:
            head = f'{self.title}'

        if self.alts:
            for alt in self.alts:
                out.append(('alt', '\n' + indent(str(alt), '    ')))

        return head + ':\n' + '\n'.join('%7s: %s' % i for i in out)

@dataclass
class PullInfo:
    """Github Pull Request Info."""
    remote: str = ''
    num: int = 0  # srcpr.num
    ver: int = 0
    pull: str = ''
    head: str = ''  # repo:branch
    created: datetime = None
    status: str = ''
    error: str = ''
    links: list[str] = field(default_factory=list)  # links to lists
    extra: list[str] = field(default_factory=list)  # extra lines in diff
    
    @property
    def title(self):
        if self.links:
            return get_list_info(self.links[0]).title
        else:
            return 'no links'

    @property
    def ref(self):
        """Return best ref for this PR."""
        # use local ref if available
        if 0 == sh_code(f'git rev-parse --verify {self.branch} >/dev/null 2>&1'):
            return f'{self.branch}'

        # fallback to special github remote pull branch
        if 0 != sh_code(f'git rev-parse --verify {self.remote_ref} >/dev/null 2>&1'):
            sh(f'git fetch -q {self.remote} refs/pull/{self.num}/head:{self.remote_ref}')

        return self.remote_ref
    
    @property
    def remote_ref(self):
        """Remote ref for PR."""
        return f'remotes/{self.remote}/pull/{self.num}/head'
    
    @property
    def branch(self):
        """Return local branch name for this PR."""
        # use head if a local PR
        if self.remote == 'origin':
            return self.head.split(':')[1]
        else:
            return f'pull/{self.remote}/{self.num}'


    def __str__(self):
        out = f'   pull: {self.pull}'
        out += f'\ncreated: {_format_time(self.created)}'
      
        counts = defaultdict(int)

        if self.head:
            out += f'\n  head: {self.head}'

        for line in self.extra:
            out += f'\n  extra: {line}'
       
        links = []

        for url in self.links:
            link = get_list_info(url)
            counts[link.status] += 1

            if link.status != 'dup' or args.dups:
                links.append('List ' + str(link))

        out += '\n  counts: ' + ' '.join(f'{k}={v}' for k,v in counts.items())
        
        for link in links:
            out += '\n\n' + link

        return out


###############################################################
# Globals

WORKDIR = Path('./work~')

DEST_REPO = Repo(owner="0ex", repo="more-awesome")

REMOTES = dict(
    origin=DEST_REPO,
    sind=Repo(owner="sindresorhus", repo="awesome"),
    emijrp=Repo(owner="emijrp", repo="awesome-awesome"),
)

FETCH_BRANCH_VER = 8

IN_PATH = 'readme.md README.md'
OUT_PATH = 'README.md'

gh = None

db = None
args = None
sess = requests.Session()

###############################################################
# Main and top-level commands

def main():
    from logging import basicConfig, getLogger, DEBUG, INFO

    basicConfig(
        level=INFO,
        format="  %(levelname)s: %(message)s",
    )
    #getLogger('urllib3.connectionpool').setLevel(INFO)
    
    print('GITHUB MIRROR:\n')

    parse_args()
    login()
    
    if args.debug:
        gh.debug = lambda req: print(req.summary())
    
    if args.verbose:
        getLogger('root').setLevel(DEBUG)

    if not (WORKDIR / '.git').exists():
        WORKDIR.mkdir(exists_ok=True, parents=True)
        sh('git clone . work')

    # start in a predictable state
    shutil.copyfile('.git/config', WORKDIR / '.git/config')
    os.chdir(WORKDIR)
    sh('git co main && git pull')

    if args.scan is not None:
        scan_pulls(args.src, args.scan)
        return
    
    if args.sort:
        sort_readme()
        return
    
    if args.untagged:
        list_untagged()
        return

    if not args.prnum:
        log('EArgs', 'No pull requests given')
        return
       
    for prnum in args.prnum:
        srcpr = Pull(args.src, prnum)

        if args.mirror:
            info = build_pull_info(srcpr)
            fetch_branch(info)
            copy_ghpr(srcpr)

        if args.info:
            show_info(srcpr, long=True, diff=not args.brief)
        
        if args.rebase:
            semantic_merge(srcpr)
            show_diff(srcpr)

        if args.accept:
            show_info(srcpr, long=True, diff=not args.brief)
            accept_pull(srcpr)
    
    log('IDone')
    
def parse_args(argv=None):
    global args

    from argparse import ArgumentParser

    p = ArgumentParser()
    
    # global behavior
    p.add_argument('-r', '--redo', type=str, default='')
    p.add_argument('-d', '--debug', action='store_true')
    p.add_argument('-v', '--verbose', action='store_true')
    p.add_argument('-t', '--tag', action='store_true', help='tag repos')
    p.add_argument('-T', '--throttle', type=int, default=0, help='throttle')
    p.add_argument('-B', '--brief', action='store_true')
    p.add_argument('--max', type=int, default=0, help='stop after max links')
    p.add_argument('--dups', action='store_true', help='no not skip dups')
    
    # repo operations
    p.add_argument('-s', '--scan', type=str, help='scan: ALL or page number')
    p.add_argument('--list', action='store_true', help='list links')
    p.add_argument('--sort', action='store_true', help='sort readme')
    p.add_argument('--fixup', action='store_true', help='fixup while sorting')
    p.add_argument('--untagged', action='store_true', help='list untagged PRs')
    p.add_argument('-S', '--src', type=str,
                   default='origin', choices=list(REMOTES), help='source repo')

    # PR operations
    p.add_argument('prnum', type=int, nargs='*', help='pull requests')
    p.add_argument('-b', '--rebase', action='store_true', help='rebase pr with main')
    p.add_argument('-a', '--accept', action='store_true', help='accept pr')
    p.add_argument('-m', '--mirror', action='store_true', help='copy branch and create pr')
    p.add_argument('-i', '--info', action='store_true', help='print PR info')

    args = p.parse_args(argv or sys.argv[1:])

def login():
    global gh, db

    db = DB()

    path = os.getenv('GITHUB_TOKEN_PATH', None) or '~/.private/github-token'

    cfg = Path(path).expanduser()
    with open(cfg) as f:
        token = f.read().strip()

    gh = GhApi(token=token)
    # print(json.dumps(gh.rate_limit.get(), indent=4))

def sort_readme():
    """Sort and normalize."""
    print('\nSORT:\n')
    
    lines = []
    topic_parts = []
    topic = False
    seen = set()

    with open(OUT_PATH) as f:
        for line in f:
            log('DLine', repr(line))
            if topic:
                if topic_parts and not line.strip():
                    # end of topic
                    topic = False
                    lines += sorted(topic_parts, key=lambda p: p.lower())
                    lines += ['\n']
                elif line.strip().startswith('-'):
                    # extract title, url, desc
                    info = parse_line(line, [topic])
                    if info:
                        log('DLink', repr(info.key), info.url, info.title)
                        if info.key in seen:
                            log('WDup', line.strip())
                            continue
                        else:
                            seen.add(info.key)

                        if args.info:
                            print(indent(str(info), '  '), '\n')

                        if args.fixup:
                            line = info.build_link() + '\n'

                    # we don't sort second-level entries
                    if line[0] == '-':
                        topic_parts.append(line)
                    else:
                        topic_parts[-1] = topic_parts[-1] + line

                elif not topic_parts:
                    # leading blank lines and text
                    lines.append(line)
                else:
                    # append sub-lists to exiting part - do not sort
                    topic_parts[-1] += line
            else:
                m = re.match(r'##+ +(.*)', line)
                if not m:
                    m = re.match(r'\*\*(.*)\*\*', line)
                if m:
                    log('ISection', m[1])
                    topic = m[1]
                    topic_parts = []
                
                lines.append(line)

    with open(OUT_PATH, 'w') as f:
        f.write(''.join(lines))

    log('ISorted')

def scan_pulls(remote, page):
    repo = REMOTES[remote]
    args = dict(
        **repo.gh,
        state='closed',
        sort='created',
        direction='asc',
    )

    if page == 'ALL':
        pulls = all_pages(gh.pulls.list, per_page=100, **args)
    else:
        pulls = gh.pulls.list(**args, page=page, per_page=20)

    for pr in pulls:
        srcpr = Pull(remote, pr.number)

        show_info(srcpr, ghpr=pr)

def list_untagged():
    """List PRs which are merged but untagged."""
    for rec in db.scan('pull/%'):
        tag = rec.get('tag')
        if not tag:
            srcpr = PullInfo(**rec)
            destpr = get_destpr(srcpr)
            dest = gh.pulls.get(**destpr.gh)
            if dest.merged:
                print(rec['key'])

def show_info(srcpr, ghpr=None, long=False, diff=True):
    """Summarize PR."""
    if not ghpr:
        ghpr = gh.pulls.get(**srcpr.gh)

    # high-level status estimation
    if ghpr.merged_at:
        status = 'merged'
    elif srcpr.repo == DEST_REPO:
        status = 'local'
    elif ghpr.user.login == srcpr.repo.owner:
        # internal PRs
        status = 'self'
    else:
        status = None

    if not status or long:
        info = build_pull_info(srcpr)
        status = info.status

    print(
        color(status, '#%-5d %7s %20.20s' % (srcpr.num, status, ghpr.head.label)),
        ghpr.title[:50])

    if long or status in ['new', 'bad']:
        print()
        print(indent(str(info), '  '))
        print()

        if diff:
            diff = sh_out(f'git diff -U1 --color=always --merge-base main {info.ref} | tail -n +5')
            print('\nDIFF:\n\n', indent(diff, '   ')) 

def fetch_branch(info: PullInfo):
    """Fetch branch from upstream into origin."""
    print('\nFETCH:\n')

    verify = sh_code(f'git rev-parse --verify {info.branch} >/dev/null 2>&1')
    if verify == 0:
        log('WExists', f'{info.branch} already exists')
        return

    sh(f'git checkout --no-guess -q -b {info.branch} {info.ref}')
    
    # remove junk commits
    ret = sh_out('git rev-list --grep="Meta tweaks" main..')
    revs = ret.strip().splitlines()
    for rev in revs:
        try:
            sh(f'git revert --no-edit {rev}')
        except Exception as e:
            log('WRevert', str(e))
 
    sh('git checkout -q main')

def add_link(info: ListInfo):
    """Add link to README."""
    lines = []
    found_line = False
    found_topic = False
    found_item = False  # seen at least one item in topic

    topic_list = info.topic.split(': ')
    extra_topic = []

    log('DInsert', info.topic, info.title)

    with open(OUT_PATH) as f:
        for line in f:
            #log('DLine', repr(line))
            if found_line:
                # just copy everything for here on out
                pass
            elif found_topic:
                # look for alphabetical location
                m = re.match(r'[-*] *(?:\[([^][]+)\]|([\w ]+))', line)
                if m:
                    pos = m[1] or m[2]
                    found_item = True
                elif found_item and not line.strip():
                    pos = 'zzz'
                else:
                    pos = None

                if extra_topic:
                    target_pos = extra_topic[0]
                else:
                    target_pos = info.title

                if pos and pos >= target_pos:
                    found_line = True
                    link = info.build_link(extra_topic[1:] if extra_topic else None)
                    if link.strip() == line.strip():
                        log('WDup', 'avoiding duplicate line')
                    elif pos == target_pos:
                        log('DInsert', 'inserting after', line.strip())
                        lines.append(line)
                        lines.append('\t' + link + '\n')
                        continue
                    else:
                        log('DInsert', 'inserting before', line.strip())
                        if extra_topic:
                            lines.append(f'- {extra_topic[0]}\n')
                            lines.append('\t' + link + '\n')
                        else:
                            lines.append(link + '\n')
            else:
                # look for matching header
                m = re.match(r'#+ *(.*)', line)
                # log('DHead', m[1] if m else 'no match')
                if not m:
                    pass
                elif m[1].lower() == 'to sort':
                    found_topic = True
                    extra_topic = topic_list
                    log('IToSort', 'missing topic', info.topic)
                else:
                    for i, part in enumerate(topic_list):
                        if m[1].lower() == part.lower():
                            found_topic = True
                            extra_topic = topic_list[i+1:]
                            log('DSection', 'found topic', info.topic, 'extra=', extra_topic)
                
            lines.append(line)

    if not found_line:
        raise RuntimeError('EBadMerge')

    with open(OUT_PATH, 'w') as f:
        f.write(''.join(lines))

def semantic_merge(srcpr):
    """Semantic Merge.

    Add new entries into markdown file manually. We do this
    because git auto-merge never works and can cause issues,
    especially if a .gitattributes file specifies `driver=union`.
    """
    print('SEMANTIC_MERGE:\n')
    info = build_pull_info(srcpr)
    destpr = get_destpr(info)

    sh(f'git checkout --no-guess -q {info.branch}')

    log('DMerge', 'attempting semantic merge')
    
    if info.error:
        log('WExtra', 'cannot manage PRs with errors')
        sh('git merge --abort')
        return
   
    #sh('rm .gitattributes', check=False)
    sh('git merge -q -X theirs --no-commit --no-stat main >/dev/null', check=False)

    sh('test -f readme.md && git rm readme.md', check=False)
    sh(f'git checkout main {OUT_PATH}')
   
    for url in info.links:
        link = get_list_info(url)
        if link.status == 'new':
            add_link(link)
  
    if 0 == sh_code('git diff --quiet'):
        log('WMerge', 'no differences')

    msg = quote(f'Merge {destpr} {info.title}')
    
    sh(f'git commit -m {msg} -a')
    sh(f'git checkout -q main && git push -q -u origin {info.branch}')
    
    log('IMerge', 'done')

def show_diff(srcpr):
    info = build_pull_info(srcpr)
    diff = sh_out(f'git diff -U1 --color=always main..{info.branch} | tail -n +5')
    print('\nDIFF:\n\n', indent(diff, '  ')) 


def copy_ghpr(srcpr):
    """Copy pull description and comments."""
    print('\nCOPY GHPR:\n')
    
    if srcpr.repo == DEST_REPO:
        log('WPullCopy', 'not copying into same repo')
        write_pull_desc(srcpr)
        return

    destpr = copy_pull_desc(srcpr)
    copy_issue_comments(srcpr, destpr)
    copy_review_comments(srcpr, destpr)
    
    dest = gh.pulls.get(**destpr.gh)
    log('IPullCopy', dest.html_url)

        
def write_pull_desc(srcpr):
    """Create pull description for existing PR."""
    pull = gh.pulls.get(**srcpr.gh)
   
    body = ''

    for line in str(build_pull_info(srcpr)).strip().splitlines():
        body += line + '\n'

    body = strip_junk(body)
    body = clean_body(srcpr, body, tag_repos=args.tag, pull_desc=args.tag)
    
    # copy original PR description above line
    above = ''
    for line in pull.body.splitlines():
        if line == '---':
            break
        else:
            above += line + '\n'

    body = above + '\n---\n\n' + body

    log('IPull', 'updating PR', pull.number)
    gh.pulls.update(
        **DEST_REPO.gh,
        pull_number=srcpr.num,
        body=body,
    )


def copy_pull_desc(srcpr) -> Pull:
    """Create or update PR in github, except comments.

    Return destpr.
    """
    ver = 6
    key = f'copy/{srcpr.key}'
    rec = db.get(key)
    
    if not rec:
        redo = 'new'
        rec = {'idmap': {}, 'ver': 0, 'tag': False}
        
    elif 'copy' in args.redo:
        redo = 'force'

    elif rec['ver'] < ver:
        redo = 'update'

    elif args.tag and not rec['tag']:
        redo = 'add-tag'

    else:
        log('DCached', key, rec)
        return Pull('origin', rec['num']) 

    # do not remove tags once added
    rec['tag'] = rec['tag'] or args.tag

    log('ICalc', redo, key, rec)
    
    orig = gh.pulls.get(**srcpr.gh)
    
    body = f'**Pull request** from @{orig.user.login}:\n\n'
    info = build_pull_info(srcpr)

    for line in str(info).strip().splitlines():
        body += line + '\n'

    if orig.body:
        body += '\n---\n' + orig.body.strip()

    body = strip_junk(body)
    body = clean_body(srcpr, body, tag_repos=rec['tag'], pull_desc=rec['tag'])

    destpr = get_destpr(info)

    if destpr:
        log('IPull', 'updating PR', destpr.num)
        gh.pulls.update(
            **destpr.gh,
            title=orig.title,
            body=body,
        )
    else:
        log('IPull', 'creating new PR', info.branch)
        sh(f'git push -q -u origin {info.branch}')

        pr = gh.pulls.create(
            **DEST_REPO.gh,
            title=orig.title,
            body=body,
            head=info.branch,  # "rajee-a:patch-1",
            base="main",
        )
        throttle()
        destpr = Pull('origin', pr.number)

    rec['num'] = destpr.num
    rec['ver'] = ver
    db.set(key, rec)

    return destpr

def get_destpr(prinfo: PullInfo):
    """Find existing destpr for srcpr."""
    pulls = gh.pulls.list(
        **DEST_REPO.gh,
        state='all',
        head=f'{DEST_REPO.owner}:{prinfo.branch}',
        sort='created',
        direction='desc',
    )
    if pulls:
        return Pull('origin', pulls[0].number)

def copy_issue_comments(srcpr, destpr):
    ver = 3
    key = f'dest/pull/{destpr.num}/comments'
    rec = db.get(key)

    if not rec:
        redo = 'new'
        rec = {'idmap': {}}
        
    elif 'comments' in args.redo:
        redo = 'force'

    elif rec.get('ver', 0) < ver:
        redo = 'update'

    elif args.tag and not rec.get('tag'):
        redo = 'add-tag'

    else:
        log('DCached', key, rec)
        return 'DONE'

    # do not remove tags once added
    rec['tag'] = rec.get('tag') or args.tag

    log('ICalc', redo, key, rec)

    if not rec['idmap']:
        # delete old comments
        for c in all_pages(gh.issues.list_comments, **destpr.gh):
            log('IDel', c.id, c.user.login)
            gh.issues.delete_comment(**destpr.gh, comment_id=c.id)

    for c in all_pages(gh.issues.list_comments, **srcpr.gh):
        new_id = rec['idmap'].get(str(c.id))
        body = attr_body(srcpr, c, tag_repos=rec['tag'])

        if new_id:
            cmt = gh.issues.update_comment(
                **destpr.gh,
                comment_id=new_id,
                body=body,
            )
        else:
            cmt = gh.issues.create_comment(
                **destpr.gh,
                body=body,
            )
            rec['idmap'][c.id] = cmt.id

        log('IEdit', 'edit' if new_id else 'new', c.id, '→', cmt.id, c.user.login)
        throttle(1)
    
    rec['ver'] = ver
    db.set(key, rec)
    
def copy_review_comments(srcpr, destpr):
    ver = 1
    key = f'dest/pull/{destpr.num}/review_comments'
    rec = db.get(key)

    if not rec:
        redo = 'new'
        rec = {'idmap': {}}
        
    elif 'comments' in args.redo:
        redo = 'force'

    elif rec.get('ver', 0) < ver:
        redo = 'update'

    elif args.tag and not rec.get('tag'):
        redo = 'add-tag'

    else:
        log('DCached', key, rec)
        return 'DONE'

    # do not remove tags once added
    rec['tag'] = rec.get('tag') or args.tag
    
    log('ICalc', redo, key, rec)

    if not rec['idmap']:
        # delete old comments
        for c in all_pages(gh.pulls.list_review_comments, **destpr.gh):
            log('IDel', c.id, c.user.login)
            gh.pulls.delete_review_comment(**destpr.gh, comment_id=c.id)
    
    for c in all_pages(gh.pulls.list_review_comments, **srcpr.gh):
        new_id = rec['idmap'].get(str(c.id))
        in_reply_to = rec['idmap'].get(str(getattr(c, 'in_reply_to_id', None)))

        body = attr_body(srcpr, c, tag_repos=rec['tag'])

        if new_id:
            cmt = gh.pulls.update_review_comment(
                **destpr.gh,
                comment_id=new_id,
                body=body,
            )
        else:
            if c.line is None and c.original_line:
                pos = dict(
                    commit_id=c.original_commit_id,
                    line=c.original_line,
                    start_line=c.original_start_line,
                )
            else:
                pos = dict(
                    commit_id=c.commit_id,
                    line=c.line,
                    start_line=c.start_line,
                )

            try:
                cmt = gh.pulls.create_review_comment(
                    **destpr.gh,
                    **pos,
                    body=body,
                    in_reply_to=in_reply_to,
                    path=c.path,
                    side=c.side,
                    start_side=c.start_side,
                )
                rec['idmap'][c.id] = cmt.id
            except HTTPError as e:
                log('WReviewComment', e)
                continue

        log('ICmt', cmt.id, cmt.user.login, cmt.line)
        throttle(1)
    
    rec['ver'] = ver
    db.set(key, rec)

def attr_body(srcpr, comment, tag_repos=False):
    when = datetime.strptime(comment.created_at, '%Y-%m-%dT%H:%M:%S%z')
    out = f'**@-{comment.user.login}** on {when:%Y-%m-%d %H:%M} says: '
    if comment.body.strip().startswith('>'):
        out += '\n'

    out += clean_body(srcpr, comment.body, tag_repos=tag_repos)
    return out

def clean_body(srcpr, body, tag_repos=False, pull_desc=False):
    """Avoid tagging users/issues for every comment.

    See "auto linked references" in github docs.
    If pull_desc is True, leave references to users.
    """
    
    # tag user in body, except sind.*
    if pull_desc:
        out = re.sub(r'@(sind.*)', r'**@-\1**', body)
    else:
        out = re.sub(r'@(\w[-\w]+)', r'**@-\1**', body)
    
    # adjust naked references to original repo
    out = re.sub(r'(?<!\w)#([0-9]+)', rf'{srcpr.repo}#\1', out)

    # strip out auto-linked references
    if not tag_repos:
        out = re.sub(r'([^ ]+)#(\d+)', r'**\1#-\2**', out)
        out = re.sub(
            r'https?://github.com/([^/ ]+)/([^/ ]+)/(?:pull|issues)/(\d+)',
            r'**\1/\2#-\3**', out)

    # always strip out refences to upstream repo in comments
    if not pull_desc:
        out = re.sub(rf'({srcpr.repo})#(\d+)', r'**\1#-\2**', out)

    return out.strip()

def test_clean_body():
    repo = Repo('xxx', 'yyy')
    srcpr = Pull(repo, 2060)
    
    # basic
    out = clean_body(srcpr, 'list: https://github.com/xxx/yyy/pull/1497')
    assert out == 'list: **xxx/yyy#-1497**'

    # with tag_repos=False
    assert 'in **xxx/yyy#-1245** but' == clean_body(srcpr, 'in #1245 but')
    assert '**but#-333**' == clean_body(srcpr, 'but#333')
    assert '**xxx/yyy#-123**' == clean_body(srcpr, '#123')
    
    # with tag_repos=True
    assert 'but#333' == clean_body(srcpr, 'but#333', tag_repos=True)
    
    # with pull_desc=False
    assert '**@-maehr**.' == clean_body(srcpr, '@maehr.')
    assert 'see **xxx/yyy#-1363**.' == clean_body(srcpr, 'see xxx/yyy#1363.')

    # with pull_desc=True
    assert '@maehr.' == clean_body(srcpr, '@maehr.', pull_desc=True)
    assert '**@-sindre.**' == clean_body(srcpr, '@sindre.', pull_desc=True)
    assert 'see xxx/yyy#1363.' == clean_body(srcpr, 'see xxx/yyy#1363.',
        tag_repos=True, pull_desc=True)
   
def strip_junk(body):
    out = body
    out = re.sub(
        r'## Requirements for your pull request.*',
        '\\[ boilerplate snipped \\]', out, flags=re.S)

    out = re.sub(
        r'#+ By submitting this pull .*',
        '\\[ boilerplate snipped \\]', out, flags=re.S)

    out = re.sub(
        r'<!-- Please fill in the .*',
        '\\[ boilerplate snipped \\]', out, flags=re.S)
    
    out = re.sub(
        r'# ALL THE BELOW CHECKBOXES .*',
        '\\[ boilerplate snipped \\]', out, flags=re.S)

    out = re.sub(
        r'[^\n]+I have read and understood the .*',
        '\\[ boilerplate snipped \\]', out, flags=re.S)

    return out

def test_strip_junk():
    from textwrap import dedent

    out = strip_junk(dedent('''
        some stuff
        ### By submitting this pull request I confirm I've read

        **Please read it multiple times. I spent a lot of time

        ## Requirements for your Awesome list

        **Go to the top and read it again.**
    '''))
    assert out.strip() == dedent('''
        some stuff
        \\[ boilerplate snipped \\]
    ''').strip()

PAT_LINK = r"""
\[([^][]+)\]
\s*
\((https?://[^()]+)\)
"""

def parse_line(line: str, topic: list[str]) -> ListInfo:
    """Parse list from line."""
    m = re.match(fr"""
        (\s*)[-*]\s*  # space, dash/star, space
        {PAT_LINK}
        (?:\s*-\s*(.*))?
        """,
        line,
        flags=re.X,
    )
    if not m:
        return None

    topic = ': '.join(topic)
    info = list_info(m[3], desc=m[4], title=m[2], topic=topic)
    info.indent = m[1]
    info.alts = []
    if not info.desc:
        info.desc = ''

    # extract alt-links
    for alt in re.finditer('.*' + PAT_LINK, info.desc, flags=re.X):
        info.alts.append(list_info(alt[2], title=m[1], desc='', topic=topic))

    return info
    

def test_parse_line():
    line = '- [Ansible](https://blah#readme) - Stuff. Also by [@jdauphant](https://blah2).'

    parse_args(['-v'])
    login()

    link = parse_line(line, ['topic'])
    print(link)

    assert link.alts

def _url_key(url: str):
    if m := re.match(r'https?://([^#?]+)', url):
        url = m[1]

    return re.sub(r'[^\w/.]+', '_', url.lower())

def _list_key(url):
    """Return key for list."""
    return f'list/{_url_key(url)}/info'

def get_list_info(url) -> ListInfo:
    """Return existing list info."""
    key = _list_key(url)
    rec = db.get(key)
    if rec:
        return ListInfo(**rec)
    else:
        return ListInfo(
            url=url,
            status='no-data',
        )


def list_info(url, *, desc, title, topic) -> ListInfo:
    """Collect info about awesome list for a URL."""
    key = _list_key(url)

    if not key:
        return ListInfo(
            url=url,
            desc=desc or '',
            title=title,
            topic=topic,
            status='bad',
        )

    ver = 3
    rec = db.get(key)

    if not rec:
        rec = dict()
        redo = 'new'
    elif rec['ver'] < ver:
        redo = 'update'
    elif 'list' in args.redo:
        redo = 'force'
    else:
        redo = None

    rec.update(ver=ver, key=key, url=url)
    out = ListInfo(**rec)

    if desc:
        out.desc = desc
    if title:
        out.title = title
    if topic:
        out.topic = topic
  
    if not isinstance(out.desc, str) or out.desc.startswith('Https'):
        out.desc = ''

    if not redo:
        return out

    log('DCalc', redo, key, str(rec)[:30] + '...')

    out.error = None
    out.status = None

    # get extra metadata about github repos
    if m := re.match(r'https?://github.com/([^/#]+)/([^/#]+)', out.url):
    
        if '#readme' not in out.url:
            out.url = out.url + '#readme'
        
        try:
            target = cache('repo/info', 1, _get_repo, m[1], m[2])
            out.stars = target.stars
            out.size = target.size
            
            if target.desc and not out.desc:
                out.desc = target.desc

            branch = cache('repo/branch', 1, _get_branch, m[1], m[2], target.branch)
            out.updated = branch.updated
        except HTTPError as e:
            out.error = 'HTTP Error: ' + str(e).splitlines()[0]
            out.status = 'bad'

    # cleanup description
    if out.desc:
        out.desc = clean_desc(out.desc)

    _check_url(out)
    
    if not out.status:
        if out.code not in [200, 0]:
            out.status = str(out.code)

    if not out.status:
        out.status = 'new'
    
    # save
    rec = asdict(out)
    db.set(key, rec)
    return out

TO_REMOVE_TITLE = """
    ^awesome-
"""

TO_REMOVE_DESC = [
    'A curated list of',
    'A curated list',
    'Curated list of',
    'Awesome list of',
    'awesome list',
    'A collection of',
    'A collaborative list of '
    'resources on',
    'collection of',
    'A collection about',
    'all awesome stuff of',
    'awesome',
    'amazing',
    'this is about useful',
    'useful resources',
    'list of',
    '[\U0001F600-\U0001F64F]',  # emoticons
    '[\U0001F300-\U0001F5FF]',  # symbols & pictographs
    '[\U0001F680-\U0001F6FF]',  # transport & map symbols
    '[\U0001F1E0-\U0001F1FF]',  # flags
    r':\w+:',  # Emoji symbols
    r'\s\s+',  # Extra space
]
    
def clean_desc(desc):
    # avoid mangling alternative links after period.
    if '.' in desc:
        desc, extra = desc.strip().split('.', 1)
    else:
        extra = ''

    for junk in TO_REMOVE_DESC:
        desc = re.sub(junk, ' ', desc, re.I)

    desc = desc.strip()
    if not desc:
        desc = 'EMPTY'

    return desc[0].upper() + desc[1:] + '.' + extra

def test_clean_desc():
    assert clean_desc('A collection about awesome blockchains') \
        == 'Blockchains.'
    
    assert clean_desc('tools and software. .') \
        == 'Tools and software. .'

def _get_repo(owner, repo):
    rec = gh.repos.get(owner, repo)
    return Record(
        stars=rec.stargazers_count,
        size=rec.size,
        desc=rec.description,
        branch=rec.default_branch,
    )

def _get_branch(owner, repo, branch):
    rec = gh.repos.get_branch(owner, repo, branch)
    return Record(updated=rec.commit.commit.committer.date)


def build_pull_info(srcpr) -> PullInfo:
    ver = 14
    key = f'info/{srcpr.key}'
    rec = db.get(key)
    
    if not rec:
        rec = dict(ver=0)
        redo = 'new'
    elif rec['ver'] < ver:
        redo = 'update'
    elif 'pull' in args.redo:
        redo = 'force'
    else:
        rec.update(num=srcpr.num)
        return PullInfo(**rec)

    log('DCalc', redo, key, rec)
    pr = gh.pulls.get(**srcpr.gh)

    out = PullInfo(num=srcpr.num, head=pr.head.label)
    out.pull = pr.html_url
    out.created = pr.created_at
    out.remote = srcpr.remote

    error = None
    topic = []
    indents = []  # for topics, except top-level

    # parse diff
    try:
        ret = sh_out(f'git diff --merge-base -U999 main {out.ref} -- {IN_PATH}')
        diff = ret.splitlines()

        for line in diff:
            if args.max and len(out.links) > args.max:
                break

            line = line.rstrip()
            if not line:
                continue

            log('DLine', line)
            
            if re.match(r'(---|\+\+\+)', line):
                # skip file headers
                continue

            # dedent
            if m := re.match(r'[+ ](\s*)', line):
                while indents and len(indents[-1]) >= len(m[1]):
                    log('DTopicEnd', topic[-1])
                    topic.pop()
                    indents.pop()

            if m := re.match(r'[+ ]#+ *(.*)', line):
                log('DSection', m[1])
                topic = [m[1]]
                indents = []
            elif line[0] == '+':
                link = parse_line(line[1:], topic)
                if link:
                    log('DLink', link.title, 'topic=', topic)
                    out.links.append(link.url)
            elif re.match(r'[-+]', line):
                out.extra.append(line)

            # topic can be "- topic" or "- [topic]...", or with star
            if m := re.match(r'[+ ](\s*)[-*] (?:([^][()]+)|\[([^][]+)\])', line):
                topic.append(m[2] or m[3])
                indents.append(m[1])
                log('DTopicAdd', topic[-1])

    except HTTPError as e:
        error = str(e)
        if args.debug:
            raise
            
    if not out.links:
        out.status = 'no-links'
    else:
        out.status = get_list_info(out.links[0]).status

    if error and not out.error:
        out.error = error

    # save
    rec = asdict(out)
    rec['ver'] = ver
    db.set(key, rec)
    return out

def _check_dup(out):
    m = re.match(r'https?://([^#?]+)', out.url)

    if not m:
        out.status = 'bad'
        return True
    
    bare_url = m[1]
    if bare_url.lower() in main_readme().lower():
        out.status = 'dup'
        return True

def cache(name, ver, func, *params):
    """Generic caching function."""
    key = f'cache/{name}/' + '/'.join(_url_key(a) for a in params)
    if raw := db.get(key):
        rec = Record(key=key, **raw)
    else:
        rec = None
    
    if not rec:
        rec = Record(ver=0)
        redo = 'new'
    elif rec['ver'] < ver:
        redo = 'update'
    elif name in args.redo:
        redo = 'force'
    else:
        return rec

    log('DCalc', redo, name, params)

    rec = func(*params)

    # save
    rec.ver = ver
    db.set(key, dict(rec))
    return rec

def fetch_url(url):
    try:
        r = sess.head(url, stream=False, allow_redirects=True)
    except (HTTPError, requests.exceptions.ConnectionError) as e:
        return Record(error=str(e))

    return Record(
        code=r.status_code,
        url=r.url,
    )

def _check_url(out):
    # no URL is unusual, but could be worth looking at
    if not out.url:
        return

    if _check_dup(out):
        return

    result = cache('fetch', 2, fetch_url, out.url)

    if e := result.get('error'):
        out.status = 'bad'
        out.code = 'URL Error: ' + str(e)
        return
       
    out.code = result['code']

    if result['url'] != out.url:
        out.redir = out.url
        out.url = result['url']
        _check_dup(out)

def accept_pull(srcpr):
    """Semantic Merge PR and prompt to accept."""
    srcinfo = build_pull_info(srcpr)
    destpr = get_destpr(srcinfo)
    dest = gh.pulls.get(**destpr.gh)
    if(dest.merged):
        log('IMerged', 'already merged', srcpr)
        return

    sh('git checkout -q main && git pull -q')

    can_ff = 0 == sh_code(f'git merge-base --is-ancestor main {srcinfo.branch}')
    if can_ff:
        log('IAccept', 'can fast-forward')
    else:
        semantic_merge(srcpr)
    
    if not args.brief:
        show_diff(srcpr)

    if confirm('merge'):
        sh(f'git merge -q --ff-only {srcinfo.branch} --no-edit')
        sh(f'git push origin main :{srcinfo.branch} && git branch -d {srcinfo.branch}')


###############################################################
# Utilities

def main_readme():
    """Return README contents from main - cache on first use."""
    global _main_readme

    try:
        return _main_readme
    except NameError:
        _main_readme = sh_out(f'git show main:{OUT_PATH}')
        return _main_readme

def confirm(question):
    ans = input(f'continue {question} (y/n) ? ')
    print()

    return ans.strip().lower().startswith('y')

def log(code, *args):
    if(code[0] == 'D'):
        level = logging.DEBUG
    elif(code[0] == 'W'):
        level = logging.WARNING
    else:
        level = logging.INFO

    logging.log(level, '%s %s', code, ' '.join(str(a) for a in args))

def color(style: str, text: str):
    """Return colored text."""
    color_codes = {
        'new': 32,
        '404': 31,
        'bad': 31,
        'merged': 90,
        'dup': 90,
        'synced': 36
    }
    code = color_codes.get(style, 36)

    return f'\x1b[{code}m{text}\x1b[0m'

class DB:
    """A simple key/value store."""

    def __init__(self):
        self.path = Path('~/.var/dev/git-mirror.db').expanduser()
        self.path.parent.mkdir(exist_ok=True, parents=True)
        self.conn = sqlite3.connect(
            self.path,
            isolation_level=None,  # autocommit mode
        )
        self.cur = self.conn.cursor()
        self.create()
        log('IOpen', self.path)

    def _encode(self, x):
        return json.dumps(x)

    def _decode(self, x):
        return json.loads(x)

    def __call__(self, sql, *args):
        self.cur.execute(sql, args)

    def create(self):
        self("""
            CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
        """)

    def set(self, key, value):
        log('DSet', str(key), self._encode(value))
        self("""REPLACE INTO kv(k,v) VALUES(?, ?)""", key, self._encode(value))

    def get(self, key):
        """Return None if doesn't exist."""
        self("""SELECT v FROM kv WHERE k = ?""", str(key))
        if row := self.cur.fetchone():
            return self._decode(row[0])
    
    def scan(self, pattern):
        """Return None if doesn't exist."""
        self("""SELECT k, v FROM kv WHERE k LIKE ? ORDER BY k""", pattern)
        for row in self.cur:
            value = self._decode(row[1])
            if not isinstance(value, dict):
                value = dict(ver=0, value=value)

            yield Record(key=row[0], **value)

def throttle(sec=1):
    """Avoid secondary rate limits."""
    if args.throttle:
        log('IThrottle', args.throttle)
        sleep(args.throttle)

def all_pages(cmd, *args, **kw):
    for page in paged(cmd, *args, **kw):
        yield from page


def sh(cmd, check=True):
    log('DSh', cmd)
    proc = run(cmd, shell=True, stdout=PIPE, stderr=STDOUT, text=True)
    for line in proc.stdout.splitlines():
        # strip out github remote garbage
        if not re.match(r'remote:', line):
            print('    ', line)

    if check and proc.returncode != 0:
        raise RuntimeError(f'process failed: {proc.returncode} != 0')

def sh_out(cmd):
    """Return stdout."""
    log('DSh', cmd)
    proc = run(cmd, shell=True, check=True, text=True, stdout=PIPE)
    return proc.stdout

def sh_code(cmd):
    """Return return code."""
    log('DSh', cmd)
    proc = run(cmd, shell=True, check=False)
    return proc.returncode

def _format_time(when):
    try:
        w = datetime.strptime(when, '%Y-%m-%dT%H:%M:%S%z')
        return f'{w:%Y-%m-%d %H:%M}'
    except ValueError:
        return 'invalid-time'

from fastcore.net import HTTP403ForbiddenError

if __name__ == '__main__':
    try:
        main()
    except HTTP403ForbiddenError as e:
        log('ERateLimit', str(e))
        log('EHeaders', json.dumps(gh.recv_hdrs))
        sys.exit(1)
    except KeyboardInterrupt:
        log('EInterrupt', 'exiting')
        sys.exit(1)


