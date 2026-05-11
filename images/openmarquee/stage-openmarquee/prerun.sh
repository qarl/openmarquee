#!/bin/bash -e
# prerun.sh — pi-gen boilerplate run before this stage's substages.
#
# The standard pi-gen pattern: copy the previous stage's rootfs into
# ROOTFS_DIR for this stage to mutate. Every custom pi-gen stage needs
# this two-liner (or pi-gen treats the stage as a no-op and skips it).
#
# Reference: pi-gen/stage1/prerun.sh -- same shape, same purpose.

if [ ! -d "${ROOTFS_DIR}" ]; then
    copy_previous
fi
