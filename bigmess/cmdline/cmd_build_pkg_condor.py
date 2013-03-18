# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the bigmess package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Just like build_pkg, but submits builds to a Condor pool.

This command performs the same actions as ``build_pkg``, but instead of
processing in a serial fashion, builds for all desired environments are
submitted individually to a Condor pool.

Currently the implmentation is as follows:

1. The source package is sent/copied to the respective execute node.
2. ``build_pkg`` is called locally on the execute machine and does any
   backporting and the actual building locally. This means that the
   ``chroot-basedir`` needs to be accessible on the execute node.
3. Build results are placed into the ``result-dir`` by ``build_pkg``
   running on the execute node, hence the specified locations have to be
   writable by the user under which the Condor job is running on the execute
   node.

For Condor pools with shared filesystems it is best to specify all file location
using absolute paths.

This implementation is experimental. A number of yet to be implemented features
will make the processing more robust. For example, per-package resource limits
for build processes.

TODO: Make an option to send the basetgz to the build node

"""

__docformat__ = 'restructuredtext'

# magic line for manpage summary
# man: -*- % just like build_pkg, but submits a build to a Condor pool

import argparse
import subprocess
from debian import deb822
import os
import sys
from os.path import join as opj
from bigmess import cfg
from .helpers import parser_add_common_args, get_build_option
from .cmd_build_pkg import _backport_dsc, _get_chroot_base
import logging
lgr = logging.getLogger(__name__)

parser_args = dict(formatter_class=argparse.RawDescriptionHelpFormatter)

def setup_parser(parser):
    from .cmd_build_pkg import setup_parser as slave_setup
    slave_setup(parser)
    parser.add_argument('--condor-request-memory', type=int, default=1000,
            help="""Memory resource limit for build jobs -- in megabyte.
            Default: 1000M""")
    parser.add_argument('--condor-nice-user', choices=('yes', 'no'),
            default='yes',
            help="""By default build jobs are submitted with the ``nice_user``
            flag, meaning they have lowest priority in the pool queue. Setting
            this to ``no`` will remove this flag and cause build jobs to have
            standard priority.""")
    parser.add_argument('--condor-logdir', metavar='PATH',
            help="""path to store Condor logfiles on the submit machine""")

def run(args):
    if args.env is None:
        # what to build for by default
        args.env = [env.split('-') for env in cfg.get('build', 'environments', default='').split()]
    lgr.debug("attempting to build in %i environments: %s" % (len(args.env), args.env))
    # post process argv to ready them for a subsequent build_pkg command
    argv = []
    i = 0
    while i < len(sys.argv):
        av = sys.argv[i]
        if av == '--env':
            # ignore, there will be individual build_pkg call per environment
            i += 2
        elif av == '--arch':
            # ignore, there will be individual build_pkg call per arch
            i += 1
            while i < len(sys.argv) - 1 and not sys.argv[i+1].startswith('-'):
                i += 1
        elif av == '--arch':
            # ignore, there will be individual build_pkg call per arch
            i += 1
        elif av == '--':
            pass
        elif av.startswith('--condor-'):
            # handled in this call
            i += 1
        elif av.startswith('--build-basedir'):
            # to be ignored for a condor submission
            i += 1
        elif av == '--backport':
            # backporting is done in this call, prior to build_pkg
            pass
        elif av == '--source-include':
            # this is handle in this call
            pass
        elif av == 'build_pkg_condor':
            argv.append('build_pkg')
        else:
            # pass on the rest
            argv.append(av)
        i += 1
    # make dsc arg explicit
    dsc_fname = argv[-1]
    argv = argv[:-1]
    dsc = deb822.Dsc(open(dsc_fname))
    settings = {
        'niceuser': args.condor_nice_user,
        'request_memory': args.condor_request_memory,
        'src_name': dsc['Source'],
        'src_version': dsc['Version'],
        'executable': argv[0]
    }
    submit = """
universe = vanilla
should_transfer_files = YES
when_to_transfer_output = ON_EXIT
getenv = True
notification = Never
transfer_executable = FALSE
request_memory = %(request_memory)i
nice_user = %(niceuser)s
executable = %(executable)s


""" % settings

    source_include = args.source_include
    for family, codename in args.env:
        # do any backports locally
        if args.backport:
            lgr.info("backporting to %s-%s" % (family, codename))
            dist_dsc_fname = _backport_dsc(dsc_fname, codename, family, args)
            # start with default for each backport run, i.e. source package version
            source_include = args.source_include
        else:
            dist_dsc_fname = dsc_fname
        if source_include is None:
            # any configure source include strategy?
            source_include = cfg.get('build', '%s source include' % family, default=False)
        dist_dsc = deb822.Dsc(open(dist_dsc_fname))
        dist_dsc_dir = os.path.dirname(dist_dsc_fname)
        # some verbosity for debugging
        submit += "\n# %s-%s\n" % (family, codename)
        # what files to transfer
        transfer_files = [dist_dsc_fname] \
                + [opj(dist_dsc_dir, f['name']) for f in dist_dsc['Files']]
        # logfile destination?
        logdir = get_build_option('condor logdir', args.condor_logdir, family, default=os.curdir)
        if not os.path.exists(logdir):
            os.makedirs(logdir)
        archs = get_build_option('architectures', args.arch, family)
        # TODO limit to default arch for arch:all packages
        if isinstance(archs, basestring):
            archs = archs.split()
        first_arch = True
        for arch in archs:
            # basetgz
            basetgz = '%s.tar.gz' % _get_chroot_base(family, codename, arch, args)
            if source_include and first_arch:
                src_incl = 'yes'
            else:
                src_incl = 'no'
            arch_settings = {
                'condorlog': os.path.abspath(logdir),
                'arch': arch,
                'arguments': ' '.join(argv[1:]
                                      + ['--env', family, codename,
                                         '--build-basedir', 'buildbase',
                                         '--arch', arch,
                                         '--chroot-basedir', '.',
                                         '--source-include', src_incl,
                                         '--']
                                      + [os.path.basename(dist_dsc_fname)]),
                'transfer_files': ','.join(transfer_files + [basetgz]),
            }
            arch_settings.update(settings)
            submit += """
# %(arch)s
arguments = %(arguments)s
transfer_input_files = %(transfer_files)s
error = %(condorlog)s/%(src_name)s_%(src_version)s_%(arch)s.$(Cluster).$(Process).err
output = %(condorlog)s/%(src_name)s_%(src_version)s_%(arch)s.$(Cluster).$(Process).out
log = %(condorlog)s/%(src_name)s_%(src_version)s_%(arch)s.$(Cluster).$(Process).log
queue
""" % arch_settings
            first_arch = False
        # stop including source for all families -- backport might reenable
        source_include = False
    # store submit file
    condor_submit = subprocess.Popen(['condor_submit'], stdin=subprocess.PIPE)
    condor_submit.communicate(input=submit)
    if condor_submit.wait():
        raise RuntimeError("could not submit build; SPEC follows\n---\n%s---\n)" % submit)
