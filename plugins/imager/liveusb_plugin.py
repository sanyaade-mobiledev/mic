#!/usr/bin/python -tt
#
# Copyright (c) 2011 Intel, Inc.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation; version 2 of the License
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc., 59
# Temple Place - Suite 330, Boston, MA 02111-1307, USA.

import os
import shutil
import tempfile

from mic import chroot, msger
from mic.utils import misc, fs_related, errors
from mic.utils.partitionedfs import PartitionedMount
from mic.conf import configmgr
from mic.plugin import pluginmgr

import mic.imager.liveusb as liveusb

from mic.pluginbase import ImagerPlugin
class LiveUSBPlugin(ImagerPlugin):
    name = 'liveusb'

    @classmethod
    def do_create(self, subcmd, opts, *args):
        """${cmd_name}: create liveusb image

        ${cmd_usage}
        ${cmd_option_list}
        """

        if not args:
            raise errors.Usage("More arguments needed")

        if len(args) != 1:
            raise errors.Usage("Extra arguments given")

        creatoropts = configmgr.create
        ksconf = args[0]

        if not os.path.exists(ksconf):
            raise errors.CreatorError("Can't find the file: %s" % ksconf)

        if creatoropts['arch'] and creatoropts['arch'].startswith('arm'):
            msger.warning('liveusb cannot support arm images, Quit')
            return

        recording_pkgs = []
        if len(creatoropts['record_pkgs']) > 0:
            recording_pkgs = creatoropts['record_pkgs']

        if creatoropts['release'] is not None:
            if 'name' not in recording_pkgs:
                recording_pkgs.append('name')
            ksconf = misc.save_ksconf_file(ksconf, creatoropts['release'])

        configmgr._ksconf = ksconf
    
        # Called After setting the configmgr._ksconf as the creatoropts['name'] is reset there.
        if creatoropts['release'] is not None:
            creatoropts['outdir'] = "%s/%s/images/%s/" % (creatoropts['outdir'], creatoropts['release'], creatoropts['name'])

        # try to find the pkgmgr
        pkgmgr = None
        for (key, pcls) in pluginmgr.get_plugins('backend').iteritems():
            if key == creatoropts['pkgmgr']:
                pkgmgr = pcls
                break

        if not pkgmgr:
            pkgmgrs = pluginmgr.get_plugins('backend').keys()
            raise errors.CreatorError("Can't find package manager: %s (availables: %s)" % (creatoropts['pkgmgr'], ', '.join(pkgmgrs)))

        creator = liveusb.LiveUSBImageCreator(creatoropts, pkgmgr)

        if len(recording_pkgs) > 0:
            creator._recording_pkgs = recording_pkgs

        if creatoropts['release'] is None:
            imagefile = "%s.usbimg" % os.path.join(creator.destdir, creator.name)
            if os.path.exists(imagefile):
                if msger.ask('The target image: %s already exists, cleanup and continue?' % imagefile):
                    os.unlink(imagefile)
                else:
                    raise errors.Abort('Canceled')

        try:
            creator.check_depend_tools()
            creator.mount(None, creatoropts["cachedir"])
            creator.install()
            creator.configure(creatoropts["repomd"])
            creator.unmount()
            creator.package(creatoropts["outdir"])
            if creatoropts['release'] is not None:
                creator.release_output(ksconf, creatoropts['outdir'], creatoropts['release'])
            creator.print_outimage_info()

        except errors.CreatorError:
            raise
        finally:
            creator.cleanup()

        msger.info("Finished.")
        return 0

    @classmethod
    def do_chroot(cls, target):
        os_image = cls.do_unpack(target)
        os_image_dir = os.path.dirname(os_image)

        # unpack image to target dir
        imgsize = misc.get_file_size(os_image) * 1024L * 1024L
        imgtype = misc.get_image_type(os_image)
        if imgtype == "btrfsimg":
            fstype = "btrfs"
            myDiskMount = fs_related.BtrfsDiskMount
        elif imgtype in ("ext3fsimg", "ext4fsimg"):
            fstype = imgtype[:4]
            myDiskMount = fs_related.ExtDiskMount
        else:
            raise errors.CreatorError("Unsupported filesystem type: %s" % fstype)

        extmnt = misc.mkdtemp()
        extloop = myDiskMount(fs_related.SparseLoopbackDisk(os_image, imgsize),
                              extmnt,
                              fstype,
                              4096,
                              "%s label" % fstype)

        try:
            extloop.mount()

        except errors.MountError:
            extloop.cleanup()
            shutil.rmtree(extmnt, ignore_errors = True)
            raise

        try:
            chroot.chroot(extmnt, None,  "/bin/env HOME=/root /bin/bash")
        except:
            raise errors.CreatorError("Failed to chroot to %s." %target)
        finally:
            chroot.cleanup_after_chroot("img", extloop, os_image_dir, extmnt)

    @classmethod
    def do_pack(cls, base_on):
        import subprocess

        def __mkinitrd(instance):
            kernelver = instance._get_kernel_versions().values()[0][0]
            args = [ "/usr/libexec/mkliveinitrd", "/boot/initrd-%s.img" % kernelver, "%s" % kernelver ]
            try:
                subprocess.call(args, preexec_fn = instance._chroot)

            except OSError, (err, msg):
               raise errors.CreatorError("Failed to execute /usr/libexec/mkliveinitrd: %s" % msg)

        def __run_post_cleanups(instance):
            kernelver = instance._get_kernel_versions().values()[0][0]
            args = ["rm", "-f", "/boot/initrd-%s.img" % kernelver]

            try:
                subprocess.call(args, preexec_fn = instance._chroot)
            except OSError, (err, msg):
               raise errors.CreatorError("Failed to run post cleanups: %s" % msg)

        convertor = liveusb.LiveUSBImageCreator()
        convertor.name = os.path.splitext(os.path.basename(base_on))[0]
        imgtype = misc.get_image_type(base_on)
        if imgtype == "btrfsimg":
            fstype = "btrfs"
        elif imgtype in ("ext3fsimg", "ext4fsimg"):
            fstype = imgtype[:4]
        else:
            raise errors.CreatorError("Unsupported filesystem type: %s" % fstyp)
        convertor._set_fstype(fstype)
        try:
            convertor.mount(base_on)
            __mkinitrd(convertor)
            convertor._create_bootconfig()
            __run_post_cleanups(convertor)
            convertor.unmount()
            convertor.package()
            convertor.print_outimage_info()
        finally:
            shutil.rmtree(os.path.dirname(base_on), ignore_errors = True)

    @classmethod
    def do_unpack(cls, srcimg):
        img = srcimg
        imgsize = misc.get_file_size(img) * 1024L * 1024L
        imgmnt = misc.mkdtemp()
        disk = fs_related.SparseLoopbackDisk(img, imgsize)
        imgloop = PartitionedMount({'/dev/sdb':disk}, imgmnt, skipformat = True)
        imgloop.add_partition(imgsize/1024/1024, "/dev/sdb", "/", "vfat", boot=False)
        try:
            imgloop.mount()
        except errors.MountError:
            imgloop.cleanup()
            raise

        # legacy LiveOS filesystem layout support, remove for F9 or F10
        if os.path.exists(imgmnt + "/squashfs.img"):
            squashimg = imgmnt + "/squashfs.img"
        else:
            squashimg = imgmnt + "/LiveOS/squashfs.img"

        tmpoutdir = misc.mkdtemp()
        # unsquashfs requires outdir mustn't exist
        shutil.rmtree(tmpoutdir, ignore_errors = True)
        misc.uncompress_squashfs(squashimg, tmpoutdir)

        try:
            # legacy LiveOS filesystem layout support, remove for F9 or F10
            if os.path.exists(tmpoutdir + "/os.img"):
                os_image = tmpoutdir + "/os.img"
            else:
                os_image = tmpoutdir + "/LiveOS/ext3fs.img"

            if not os.path.exists(os_image):
                raise errors.CreatorError("'%s' is not a valid live CD ISO : neither "
                                          "LiveOS/ext3fs.img nor os.img exist" %img)
            imgname = os.path.basename(srcimg)
            imgname = os.path.splitext(imgname)[0] + ".img"
            rtimage = os.path.join(tempfile.mkdtemp(dir = "/var/tmp", prefix = "tmp"), imgname)
            shutil.copyfile(os_image, rtimage)

        finally:
            imgloop.cleanup()
            shutil.rmtree(tmpoutdir, ignore_errors = True)
            shutil.rmtree(imgmnt, ignore_errors = True)

        return rtimage
