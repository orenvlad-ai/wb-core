# Web source publication rollback image registration

The root-storage inventory recognizes only an exact registered operation image.
There is no `state/evidence/*.before.json` allowlist. Recognition grants no root
write admission and no retention/deletion permission: images remain protected by
`verified_reconciliation_evidence` until exact terminal reconciliation and a later
reviewed lifecycle operation.

The native `WebSourcePublicationAdapter` registers a newly verified durable backup
before the first business-data write. A small owner-only sidecar binds the image
path, operation, phase, request/source hashes, independently previewed candidate
and prestate digests, full image SHA256 and file identity (device, inode, size,
mtime and ctime). Status reads at most 64 KiB plus file metadata. It never parses
or hashes the rollback body. Any image replacement, mutation, missing/invalid
sidecar, symlink or unsafe sidecar permissions leaves the image unregistered.

Sidecars live in `state/evidence/.web-source-publication-rollback/` (0700), with
one `<operation_id>.json` file (0600), owned by the image owner. Publication uses a
fully fsynced temporary file and atomic no-replace link, then directory fsync.
A crash before publication leaves no final claim; a crash after publication leaves
a complete claim or no durable claim. Temporary `.claim-*` files are ignored.
An existing different claim is never overwritten. If registration interrupts a
native apply, use operation readback and this registration tool; never resubmit a
business-data operation to obtain its sidecar.

## Existing WBC 0208 image, before first candidate preflight/release

Use the verified candidate source tree containing this tool, under the image's
owning account. `WBC_CANDIDATE_ROOT` must identify that verified source tree; it
need not be the installed application. The tool imports only from that tree.
The first invocation is read-only and must return `verified_read_only`:

```sh
python3 "$WBC_CANDIDATE_ROOT/apps/web_source_rollback_registration.py" \
  --runtime-dir /opt/wb-core-runtime/state \
  --image /opt/wb-core-runtime/state/evidence/wbc0208-n27-16-21-publication.before.json \
  --receipt /opt/wb-core-runtime/state/private-evidence/wbc0208-nov27/16-21/publication-apply.json \
  --expected-image-sha256 8baa42b5c18081937aa2e79eaf3cb582bc7ad3a462b60e63fcd8cbea06735487
```

After the independently captured receipt and pinned image pass, use the exact
same command with `--register` appended. This writes only the small registration
sidecar, preserving the image and all business stores. It performs a one-time
full validation again before publication; identical existing registration is
idempotent. Expected result: `registered`, with registration path
`/opt/wb-core-runtime/state/evidence/.web-source-publication-rollback/wbc0208-n27-16-21-publication.json`.

Then run the candidate's read-only root-storage preflight/status and confirm that
this exact image is owned by `web_source_publication_rollback`. Every remaining
unregistered file or other storage violation still blocks release. The installed
older policy cannot recognize this new registration contract; its failure must
not be bypassed by a wildcard or ignored return code. Use the separately reviewed
candidate-aware installer transition, then verify installed status normally.
