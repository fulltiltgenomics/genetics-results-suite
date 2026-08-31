import hashlib
import os
import threading
import time
import types

from .harness import _LogCapture, check, expect_request_error, skip, sup


def test_manifest(tmp):
    d = os.path.join(tmp, "artifacts")
    os.makedirs(d)
    with open(os.path.join(d, "plot.png"), "wb") as fh:
        fh.write(b"\x89PNG" * 4)
    with open(os.path.join(d, "table.csv"), "w") as fh:
        fh.write("a,b\n")
    with open(os.path.join(d, "trailing.png "), "w") as fh:
        fh.write("x")
    with open(os.path.join(d, "new\nline.txt"), "w") as fh:
        fh.write("x")
    os.makedirs(os.path.join(d, "subdir"))
    with open(os.path.join(d, "subdir", "hidden.txt"), "w") as fh:
        fh.write("x")
    os.symlink("/etc/passwd", os.path.join(d, "link.txt"))
    os.link(os.path.join(d, "table.csv"), os.path.join(d, "hardlink.csv"))

    entries, omitted, digests = sup.build_manifest(d)
    names = [e["name"] for e in entries]
    check("manifest: lists plain regular files", names == ["plot.png"], f"got {names}")
    check("manifest: content_type from the name",
          entries and entries[0]["content_type"] == "image/png")
    check("manifest: size from fstat", entries and entries[0]["size"] == 16)
    check("manifest: omits and counts the rest", omitted == 6, f"got {omitted}")
    check("manifest: no path, no execution id, no url",
          all(set(e) == {"name", "size", "content_type"} for e in entries))

    # Every name build_manifest withheld must also be unreadable, and for the same reason:
    # the two run the same checks so the manifest never advertises what the read refuses,
    # and the read never serves what the manifest hid.
    data, ctype = sup.read_artifact_bytes(d, "plot.png", expected_digests=None)
    check("artifact read: returns the bytes and the name's content type",
          data == b"\x89PNG" * 4 and ctype == "image/png", f"got {len(data)} {ctype}")
    for name, status in (
        ("link.txt", 404),          # symlink
        ("hardlink.csv", 404),      # st_nlink != 1
        ("subdir", 404),            # not a regular file
        ("absent.png", 404),
        ("trailing.png ", 400),     # _name_is_retrievable
        ("../../etc/passwd", 400),
        ("new\nline.txt", 400),
        ("", 400),
    ):
        expect_request_error(f"artifact read: refuses {name!r}",
                             lambda n=name: sup.read_artifact_bytes(d, n, expected_digests=None),
                             status, "NotFound" if status == 404 else "InvalidRequest")

    check("manifest: only the listed name is hashed, and it is hashed",
          set(digests) == {"plot.png"} and digests["plot.png"] ==
          hashlib.sha256(b"\x89PNG" * 4).hexdigest(), f"got {digests}")

    with open(os.path.join(d, "big.bin"), "wb") as fh:
        fh.write(b"x" * 100)
    expect_request_error("artifact read: an oversize artifact is 413, not a truncated body",
                         lambda: sup.read_artifact_bytes(d, "big.bin", max_bytes=99,
                                                        expected_digests=None),
                         413, "ArtifactTooLarge")
    # Over the REAL read cap, not a test-local one: build_manifest hashes with the default, so
    # a 100-byte file with max_bytes=99 would not exercise the branch.
    with open(os.path.join(d, "huge.bin"), "wb") as fh:
        fh.write(b"x" * (sup.ARTIFACT_READ_MAX_BYTES + 1))
    over = sup.build_manifest(d, max_entries=10)[2]
    check("manifest: a file over the read cap is listed but has no digest, because it can "
          "never be served and a truncation must not make it servable",
          "huge.bin" in over and over["huge.bin"] is None, f"got {over.get('huge.bin')!r}")
    expect_request_error(
        "artifact read: a listed-but-unhashable file is refused, not served",
        lambda: sup.read_artifact_bytes(d, "big.bin", expected_digests={"big.bin": None}),
        409, "ArtifactModified")

    # The fail-open case must not be reachable by FORGETTING the argument: a future caller that
    # omits it would otherwise serve unverified bytes with no log line to show for it.
    try:
        sup.read_artifact_bytes(d, "plot.png")
    except TypeError:
        omitted_raises = True
    except Exception as exc:
        omitted_raises = f"raised {type(exc).__name__}"
    else:
        omitted_raises = "served the bytes"
    check("artifact read: omitting expected_digests raises rather than disabling the integrity "
          "binding — None disables it and has to be written",
          omitted_raises is True, f"got {omitted_raises}")


def test_artifact_integrity(tmp):
    """The retention window must not serve bytes that moved.

    The tampering is done by the harness, at the harness's own uid, and that is the threat model
    rather than a shortcut: /scratch is writable by any process at the shared uid 65532, and
    routing the write through a forked child would test the fork rather than the control. What
    is under test is what the SUPERVISOR does with a directory altered behind it.

    Every assertion carries its negative control in the same breath: the same read with the
    binding disabled must SERVE the attacker's bytes. Without that, an accidentally-empty
    artifacts directory would make the whole group pass.
    """
    root = os.path.join(tmp, "integrity")
    os.makedirs(root)
    eid = "22222222-2222-4222-8222-222222222222"
    dirs = sup.ExecutionDirs(root, eid)
    dirs.create()
    victim = b"SECRET-VICTIM-DATA"
    with open(os.path.join(dirs.artifacts, "private.csv"), "wb") as fh:
        fh.write(victim)

    s = sup.Supervisor(root, ready=True)
    entries, _, digests = sup.build_manifest(dirs.artifacts)
    s._retention[eid] = [time.monotonic() + 900, 0]
    s._record_digests(eid, digests)
    s._retained_ids.add(eid)

    data, ctype = s.read_artifact(eid, "private.csv")
    check("artifact integrity: an untouched artifact is still served",
          data == victim and ctype == "text/csv", f"got {data!r} {ctype}")

    # SAME LENGTH, so that nothing can pass by comparing sizes: st_size is unchanged and the
    # manifest chat-backend already holds still says 18 bytes.
    forged = b"ATTACKER-OWNED-YOU"
    check("artifact integrity: the probe's overwrite keeps the size identical",
          len(forged) == len(victim))
    with open(os.path.join(dirs.artifacts, "private.csv"), "wb") as fh:
        fh.write(forged)
    expect_request_error("artifact integrity: an OVERWRITTEN artifact is refused, not served",
                         lambda: s.read_artifact(eid, "private.csv"), 409, "ArtifactModified")
    served, _ = sup.read_artifact_bytes(dirs.artifacts, "private.csv", expected_digests=None)
    check("artifact integrity: NEGATIVE CONTROL — with the binding disabled the same read "
          "hands back the attacker's bytes",
          served == forged, f"got {served!r}")

    with open(os.path.join(dirs.artifacts, "planted.csv"), "wb") as fh:
        fh.write(b"PLANTED")
    entries_now, _, _ = sup.build_manifest(dirs.artifacts)
    check("artifact integrity: the planted file really is on disk and would be listed by a "
          "manifest built now",
          "planted.csv" in {e["name"] for e in entries_now})
    expect_request_error("artifact integrity: a PLANTED artifact — one no manifest ever "
                         "listed — is refused",
                         lambda: s.read_artifact(eid, "planted.csv"), 404, "NotFound")
    served, _ = sup.read_artifact_bytes(dirs.artifacts, "planted.csv", expected_digests=None)
    check("artifact integrity: NEGATIVE CONTROL — with the binding disabled the planted file "
          "is served",
          served == b"PLANTED", f"got {served!r}")

    # An execution retained by _register_retention rather than by _retain — an exception before
    # build_manifest ever ran — advertised nothing, so it serves nothing.
    other = "33333333-3333-4333-8333-333333333333"
    odirs = sup.ExecutionDirs(root, other)
    odirs.create()
    with open(os.path.join(odirs.artifacts, "orphan.csv"), "wb") as fh:
        fh.write(b"x")
    s._register_retention(other, odirs)
    s._retained_ids.add(other)
    expect_request_error("artifact integrity: an execution retained without a manifest serves "
                         "nothing at all",
                         lambda: s.read_artifact(other, "orphan.csv"), 404, "NotFound")
    expect_request_error("artifact integrity: and it still answers the name rules first, so "
                         "the digest map cannot mask a 400",
                         lambda: s.read_artifact(other, "../orphan.csv"), 400, "InvalidRequest")

    s._forget_retained(eid)
    check("artifact integrity: forgetting a retained execution drops its digest map too",
          eid not in s._artifact_digests, f"{sorted(s._artifact_digests)}")


ENV_NO_SEAL = "SUPERVISOR_TEST_NO_SEAL"


ENV_SEAL_NO_AAD = "SUPERVISOR_TEST_SEAL_NO_AAD"


ENV_SEAL_NO_EID_AAD = "SUPERVISOR_TEST_SEAL_NO_EID_AAD"


ENV_SEAL_KEEPS_PLAINTEXT = "SUPERVISOR_TEST_SEAL_KEEPS_PLAINTEXT"


ENV_SEAL_NO_RELEASE_PURGE = "SUPERVISOR_TEST_SEAL_NO_RELEASE_PURGE"


def _unsealed_retained(self, job):
    """_seal_retained as the completion path was before artifact encryption. The control.

    Nothing is encrypted, nothing is purged and no key is kept, and `None` tells build_manifest
    to hash the directory from disk. `job.sealed` is still set, because the pre-fix path set no
    such flag and _release must not react by emptying the directory — that would make the
    control test _secure_unsealed instead of the seal.
    """
    job.sealed = True
    return None, 0, True


def _aad_without_name(execution_id, name):
    """artifact_aad with the NAME dropped, so every artifact of one execution is sealed under
    the same associated data and their ciphertexts become interchangeable. The control for the
    NAME half of the binding, which is what makes a relabel fail rather than succeed."""
    return execution_id.encode("utf-8")


def _aad_without_execution_id(execution_id, name):
    """artifact_aad with the EXECUTION ID dropped, so the same name in two executions seals
    under the same associated data. The control for the other half of the binding.

    _aad_without_name does not cover it: that control still binds the execution id, so the
    "lifted into another execution" half went untested. Two env vars rather than one, because a
    control that drops both cannot show which half each assertion rests on.
    """
    return name.encode("utf-8")


def _purge_that_keeps_plaintext(artifacts_dir):
    """_purge_artifacts with the deletion removed: it counts what it found and leaves it on
    disk. The control for the fail-closed arm.

    It returns `emptied=False` because that is the truth about what it did. A control that lied
    in the second slot would exercise the log line rather than the plumbing.
    """
    return sum(1 for _ in sup._iter_dir_names(artifacts_dir, sup.TRIM_ENTRY_CEILING)), False


def _release_without_purge(self, job):
    """_secure_unsealed with the emptying removed. The control for the structural half of the
    property: with it installed, an execution that raises before the seal retains its whole
    directory in the clear for RETENTION_S, which is the original demonstrated attack."""
    return None


def _fake_job(root, execution_id):
    """What _seal_retained reads off a job: its execution id and its directories."""
    dirs = sup.ExecutionDirs(root, execution_id)
    dirs.create()
    return types.SimpleNamespace(dirs=dirs,
                                 req=types.SimpleNamespace(execution_id=execution_id))


def _write(path, data):
    with open(path, "wb") as fh:
        fh.write(data)


def test_artifact_encryption(tmp):
    """A RETAINED artifact must not be plaintext on disk.

    The demonstrated attack is a second execution's child doing listdir(/scratch) and reading a
    peer's artifacts. What is asserted here is what closes: after the seal pass, the bytes a
    same-uid reader finds on disk are not the bytes the script wrote. THE LIVE WINDOW IS NOT
    CLOSED and is not asserted about — the child writes plaintext with a raw open() while it
    runs. The reads are done at the harness's own uid for test_artifact_integrity's reason.

    Five negative controls, because most of these checks would pass vacuously on an empty or
    unsealed directory: SUPERVISOR_TEST_NO_SEAL=1 restores the pre-encryption completion path,
    SUPERVISOR_TEST_SEAL_NO_AAD=1 drops the NAME from the associated data,
    SUPERVISOR_TEST_SEAL_NO_EID_AAD=1 drops the EXECUTION ID, SUPERVISOR_TEST_SEAL_KEEPS_
    PLAINTEXT=1 removes the fail-closed destruction, and SUPERVISOR_TEST_SEAL_NO_RELEASE_PURGE=1
    removes the emptying of a directory retained without ever having been sealed.
    """
    no_seal = os.environ.get(ENV_NO_SEAL) == "1"
    no_aad = os.environ.get(ENV_SEAL_NO_AAD) == "1"
    no_eid_aad = os.environ.get(ENV_SEAL_NO_EID_AAD) == "1"
    keeps_plaintext = os.environ.get(ENV_SEAL_KEEPS_PLAINTEXT) == "1"
    no_release_purge = os.environ.get(ENV_SEAL_NO_RELEASE_PURGE) == "1"
    seal_suffix = (" (SUPERVISOR_TEST_NO_SEAL=1 is installed: this is the control)"
                   if no_seal else "")
    aad_suffix = ((" (SUPERVISOR_TEST_SEAL_NO_AAD=1 is installed: this is the control)"
                   if no_aad else "")
                  + (" (SUPERVISOR_TEST_SEAL_NO_EID_AAD=1 is installed: this is the control)"
                     if no_eid_aad else ""))
    purge_suffix = (" (SUPERVISOR_TEST_SEAL_KEEPS_PLAINTEXT=1 is installed: this is the "
                    "control)" if keeps_plaintext else "")
    release_suffix = (" (SUPERVISOR_TEST_SEAL_NO_RELEASE_PURGE=1 is installed: this is the "
                      "control)" if no_release_purge else "")

    real_seal_retained = sup.Supervisor._seal_retained
    real_secure_unsealed = sup.Supervisor._secure_unsealed
    real_aad = sup.artifact_aad
    real_purge = sup._purge_artifacts
    real_seal_artifact = sup.seal_artifact
    real_seal_retained_artifacts = sup.seal_retained_artifacts
    if no_seal:
        sup.Supervisor._seal_retained = _unsealed_retained
    if no_aad:
        sup.artifact_aad = _aad_without_name
    if no_eid_aad:
        sup.artifact_aad = _aad_without_execution_id
    if keeps_plaintext:
        sup._purge_artifacts = _purge_that_keeps_plaintext
    if no_release_purge:
        sup.Supervisor._secure_unsealed = _release_without_purge
    try:
        root = os.path.join(tmp, "sealed")
        os.makedirs(root)
        s = sup.Supervisor(root, ready=True)

        # -- the pass itself, over a directory holding what a real child can leave behind ----
        eid = "44444444-4444-4444-8444-444444444444"
        job = _fake_job(root, eid)
        art = job.dirs.artifacts
        secret = b"SECRET-VICTIM-DATA,1\n"
        _write(os.path.join(art, "private.csv"), secret)
        _write(os.path.join(art, "second.csv"), b"another,2\n")
        # Four things build_manifest omits and read_artifact can never address, and that are
        # therefore pure retained plaintext with no reader: a subdirectory's contents, a
        # symlink, a hard link to a file outside the tree, and a name with a control character.
        os.makedirs(os.path.join(art, "subdir"))
        _write(os.path.join(art, "subdir", "hidden.txt"), b"HIDDEN-PLAINTEXT")
        os.symlink("/etc/passwd", os.path.join(art, "link.txt"))
        _write(os.path.join(root, "outside.csv"), b"OUTSIDE-PLAINTEXT")
        os.link(os.path.join(root, "outside.csv"), os.path.join(art, "hardlink.csv"))
        _write(os.path.join(art, "new\nline.txt"), b"CONTROL-CHAR-PLAINTEXT")
        # A ZERO-BYTE ARTIFACT is an ordinary output, not an edge case somebody contrived: an
        # empty result frame from to_csv, a log nothing wrote to. It seals to a bare envelope
        # and it is the case the read path used to die on.
        _write(os.path.join(art, "empty.bin"), b"")

        s._retention[eid] = [time.monotonic() + 900, 100]
        sealed, purged, secured = s._seal_retained(job)

        left = sorted(os.listdir(art))
        check("artifact seal: what no read path can ever address is deleted rather than "
              "retained in the clear — a subdirectory, a symlink, a hard link and an "
              "unretrievable name all go, and are counted into artifacts_omitted",
              left == ["empty.bin", "private.csv", "second.csv"] and purged == 4,
              f"left {left}, purged {purged}" + seal_suffix)
        check("artifact seal: the pass reports that the directory is secured, which is the "
              "only thing that means 'no plaintext was left behind'",
              secured is True, f"secured {secured!r}" + seal_suffix)

        with open(os.path.join(art, "private.csv"), "rb") as fh:
            on_disk = fh.read()
        check("artifact seal: THE PROPERTY — a same-uid reader of a retained artifact gets "
              "the sealed envelope, not the bytes the script wrote",
              secret not in on_disk and on_disk[:len(secret)] != secret,
              f"read {on_disk[:40]!r} back off disk" + seal_suffix)
        check("artifact seal: the envelope costs exactly a nonce and a tag",
              len(on_disk) == len(secret) + sup.ARTIFACT_ENVELOPE_BYTES,
              f"{len(on_disk)} bytes on disk for {len(secret)} of plaintext" + seal_suffix)
        check("artifact seal: the pass reports the PLAINTEXT size and the PLAINTEXT digest, "
              "measured while the plaintext still existed",
              sealed and sealed.get("private.csv")
              == (len(secret), hashlib.sha256(secret).hexdigest()),
              f"got {sealed.get('private.csv') if sealed else sealed!r}" + seal_suffix)

        entries, omitted, digests = sup.build_manifest(art, sealed=sealed)
        by_name = {e["name"]: e for e in entries}
        check("artifact seal: the manifest still describes the PLAINTEXT — same size, same "
              "digest — so nothing downstream changes meaning because the file grew",
              by_name.get("private.csv", {}).get("size") == len(secret)
              and digests.get("private.csv") == hashlib.sha256(secret).hexdigest(),
              f"got {by_name.get('private.csv')} {digests.get('private.csv')!r}")

        s._record_digests(eid, digests)
        s._retained_ids.add(eid)
        key = s._artifact_keys.get(eid)
        check("artifact seal: the key is a MUTABLE buffer, so it can be wiped in place rather "
              "than rebound",
              no_seal or (isinstance(key, bytearray) and len(key) == sup.ARTIFACT_KEY_BYTES),
              f"got {type(key).__name__}" + seal_suffix)
        # Not asserted for these files: the cached size is charged in st_blocks, and a 21-byte
        # artifact and its 49-byte envelope occupy the same block, so the honest growth is 0.

        data, ctype = s.read_artifact(eid, "private.csv")
        check("artifact seal: the read path opens it again and hands back the plaintext",
              data == secret and ctype == "text/csv", f"got {data!r} {ctype}")

        # The zero-byte boundary, which the size group below never pinned and which is where
        # the read broke: seal_artifact handled it, open_artifact raised ValueError out of the
        # ctypes layer, and no handler on the way to the socket caught that type.
        if no_seal:
            skip("artifact seal: a ZERO-BYTE artifact seals to a bare envelope and is "
                 "advertised with the digest of nothing",
                 "SUPERVISOR_TEST_NO_SEAL=1 builds no seal map")
            skip("artifact seal: and a zero-byte sealed artifact is exactly the envelope on "
                 "disk", "SUPERVISOR_TEST_NO_SEAL=1 seals nothing")
        else:
            check("artifact seal: a ZERO-BYTE artifact seals to a bare envelope and is "
                  "advertised with the digest of nothing",
                  sealed.get("empty.bin") == (0, hashlib.sha256(b"").hexdigest()),
                  f"got {sealed.get('empty.bin')!r}")
            check("artifact seal: and a zero-byte sealed artifact is exactly the envelope on "
                  "disk",
                  os.path.getsize(os.path.join(art, "empty.bin"))
                  == sup.ARTIFACT_ENVELOPE_BYTES,
                  f"{os.path.getsize(os.path.join(art, 'empty.bin'))} bytes")
        empty_read = None
        try:
            empty_read = s.read_artifact(eid, "empty.bin")
        except Exception as exc:                     # noqa: BLE001 — the point is the TYPE
            empty_read = exc
        check("artifact seal: THE 0-BYTE READ — an empty artifact the manifest advertised "
              "opens and returns b'', rather than raising a type no handler catches and "
              "killing the connection with no status line",
              empty_read == (b"", "application/octet-stream"),
              f"got {empty_read!r}")

        if no_seal:
            # EVERY check the else branch owns is recorded, not only the first. Four of them
            # used to simply not execute under this control with nothing in the output to say
            # so, which is how a mode ends up proving less than its summary line claims.
            for name, why in (
                ("artifact seal: a sealed file MOVED to another name inside the same "
                 "execution is refused", "leaves nothing sealed to move"),
                ("artifact seal: a sealed artifact does not open under a name, or an "
                 "execution id, it was not sealed for", "seals nothing to bind an AAD to"),
                ("artifact seal: another execution's key does not open it",
                 "mints no key for a wrong one to be substituted for"),
                ("artifact seal: a file planted AFTER the pass is not in the seal map",
                 "builds no seal map for a planted file to be absent from"),
                ("artifact seal: forgetting a retained execution WIPES its key in place",
                 "keeps no key to wipe"),
            ):
                skip(name, f"SUPERVISOR_TEST_NO_SEAL=1 {why}")
        else:
            first = os.path.join(art, "private.csv")
            second = os.path.join(art, "second.csv")
            with open(first, "rb") as fh:
                a_bytes = fh.read()
            with open(second, "rb") as fh:
                b_bytes = fh.read()
            _write(first, b_bytes)
            _write(second, a_bytes)
            expect_request_error(
                "artifact seal: a sealed file MOVED to another name inside the same execution "
                "is refused",
                lambda: s.read_artifact(eid, "private.csv"), 409, "ArtifactModified")
            _write(first, a_bytes)
            _write(second, b_bytes)

            # The name binding is asserted on the primitive, not through read_artifact: a
            # swapped file is refused there whether or not the name is in the associated data,
            # because the plaintext digest catches it too. What only the AAD catches is a
            # ciphertext opening under a name, or an execution, it was not sealed for.
            aad_dir = os.path.join(tmp, "aad")
            os.makedirs(aad_dir, exist_ok=True)
            _write(os.path.join(aad_dir, "one.csv"), b"BOUND-TO-ITS-OWN-NAME\n")
            probe_key = sup.new_artifact_key()
            adfd = os.open(aad_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                sup.seal_artifact(adfd, "one.csv", probe_key, sup.artifact_aad(eid, "one.csv"))
            finally:
                os.close(adfd)
            with open(os.path.join(aad_dir, "one.csv"), "rb") as fh:
                one_blob = fh.read()
            other_eid = "88888888-8888-4888-8888-888888888888"
            moved = []
            for label, aad in (("another name", sup.artifact_aad(eid, "two.csv")),
                               ("another execution",
                                sup.artifact_aad(other_eid, "one.csv"))):
                try:
                    sup.open_artifact(one_blob, probe_key, aad)
                except sup.ArtifactCryptoError:
                    continue
                moved.append(label)
            sup.wipe_artifact_key(probe_key)
            check("artifact seal: a sealed artifact does not open under a name, or an "
                  "execution id, it was not sealed for — both are bound into the associated "
                  "data, so a ciphertext cannot be relabelled or lifted between executions",
                  not moved, f"opened under {moved}" + aad_suffix)

            other_key = sup.new_artifact_key()
            s._artifact_keys[eid] = other_key
            expect_request_error(
                "artifact seal: another execution's key does not open it",
                lambda: s.read_artifact(eid, "private.csv"), 409, "ArtifactModified")
            sup.wipe_artifact_key(other_key)
            s._artifact_keys[eid] = key

            planted = os.path.join(art, "planted.csv")
            _write(planted, b"PLANTED-BY-A-PEER")
            entries_now, _, _ = sup.build_manifest(art, sealed=sealed)
            check("artifact seal: a file planted AFTER the pass is not in the seal map, so the "
                  "manifest omits it rather than listing something it cannot open",
                  "planted.csv" not in {e["name"] for e in entries_now},
                  f"got {[e['name'] for e in entries_now]}")
            os.unlink(planted)

            key_object = key
            s._forget_retained(eid)
            check("artifact seal: forgetting a retained execution WIPES its key in place and "
                  "drops it, so the key never outlives the entry it belongs to",
                  eid not in s._artifact_keys
                  and key_object == bytearray(sup.ARTIFACT_KEY_BYTES),
                  f"{eid in s._artifact_keys}, key {bytes(key_object)!r}")

        # -- the read cap applies to the PLAINTEXT, at the boundary ------------------------
        cap = sup.ARTIFACT_READ_MAX_BYTES
        for size, want_status in ((cap, None), (cap + 1, 413)):
            beid = ("55555555-5555-4555-8555-555555555555" if want_status is None
                    else "66666666-6666-4666-8666-666666666666")
            bjob = _fake_job(root, beid)
            _write(os.path.join(bjob.dirs.artifacts, "big.bin"), b"z" * size)
            s._retention[beid] = [time.monotonic() + 900, 0]
            bsealed, _, _ = s._seal_retained(bjob)
            if want_status is None:
                check("artifact seal: the envelope growth is added back into the cached "
                      "retained size, so RETAINED_ARTIFACTS_CEILING is not enforced against "
                      "a pre-seal number (this artifact is a whole number of blocks, so the "
                      "envelope really does cost another one)",
                      no_seal or s._retention[beid][1] > 0,
                      f"cached size {s._retention[beid][1]}" + seal_suffix)
            _, _, bdigests = sup.build_manifest(bjob.dirs.artifacts, sealed=bsealed)
            s._record_digests(beid, bdigests)
            s._retained_ids.add(beid)
            if want_status is None:
                got, _ = s.read_artifact(beid, "big.bin")
                check("artifact seal: an artifact of EXACTLY ARTIFACT_READ_MAX_BYTES is still "
                      "served — the cap bounds the response, so it is charged against the "
                      "plaintext and the envelope does not push it over",
                      len(got) == cap, f"got {len(got)} bytes for a {size}-byte artifact")
            else:
                expect_request_error(
                    "artifact seal: one byte over the cap is still 413, so sealing did not "
                    "move the boundary in either direction",
                    lambda: s.read_artifact(beid, "big.bin"), 413, "ArtifactTooLarge")

        # -- fail closed, LOCALISED: one unsealable file does not destroy the rest ----------
        # The pass used to raise on the first file it could not seal and the caller destroyed
        # the execution's whole output. chmod is contrived; ENOSPC is not.
        feid = "77777777-7777-4777-8777-777777777777"
        fjob = _fake_job(root, feid)
        for n in range(3):
            _write(os.path.join(fjob.dirs.artifacts, f"f{n}.csv"), b"PLAINTEXT-%d\n" % n)
        s._retention[feid] = [time.monotonic() + 900, 0]
        calls = []

        def failing_seal(dfd, name, key_, aad, chunk_bytes=sup.CRYPT_CHUNK_BYTES):
            calls.append(name)
            if len(calls) == 2:
                raise sup.ArtifactCryptoError("simulated libcrypto failure")
            return real_seal_artifact(dfd, name, key_, aad, chunk_bytes)

        sup.seal_artifact = failing_seal
        try:
            fsealed, fomitted_n, fsecured = s._seal_retained(fjob)
        finally:
            sup.seal_artifact = real_seal_artifact
        victim = calls[1] if len(calls) > 1 else None
        remaining = sorted(os.listdir(fjob.dirs.artifacts))
        clear = []
        for name in remaining:
            with open(os.path.join(fjob.dirs.artifacts, name), "rb") as fh:
                if b"PLAINTEXT-" in fh.read():
                    clear.append(name)
        check("artifact seal: FAIL CLOSED — a file that could not be sealed leaves no "
              "plaintext behind, because the alternative is retaining exactly what the seal "
              "exists to remove",
              not clear, f"still in the clear: {clear}" + seal_suffix + purge_suffix)
        localised_name = ("artifact seal: LOCALISED — the one unsealable file is deleted and "
                          "the execution's OTHER artifacts are sealed and still listed, so a "
                          "single failure does not destroy an output the caller has already "
                          "paid for")
        manifest_name = ("artifact seal: LOCALISED — the manifest advertises the survivors "
                         "and not the one that went, so no caller is told about an artifact "
                         "it cannot have")
        if no_seal:
            skip(localised_name, "SUPERVISOR_TEST_NO_SEAL=1 seals nothing to survive")
        else:
            check(localised_name,
                  victim is not None and victim not in remaining
                  and len(remaining) == 2 and set(fsealed) == set(remaining),
                  f"remaining {remaining}, victim {victim!r}, sealed {sorted(fsealed)}")
        check("artifact seal: LOCALISED — the deleted file is counted into "
              "artifacts_omitted, so it does not vanish silently either",
              fomitted_n == 1, f"got {fomitted_n}" + seal_suffix + purge_suffix)
        check("artifact seal: LOCALISED — and the pass still reports the directory secured, "
              "because everything that is not sealed is gone",
              fsecured is True, f"secured {fsecured!r}" + seal_suffix + purge_suffix)
        fentries, _, fdigests = sup.build_manifest(fjob.dirs.artifacts, sealed=fsealed)
        if no_seal:
            skip(manifest_name, "SUPERVISOR_TEST_NO_SEAL=1 builds no seal map to filter by")
        else:
            check(manifest_name,
                  {e["name"] for e in fentries} == set(remaining)
                  and victim not in fdigests,
                  f"got {[e['name'] for e in fentries]}")

        # -- fail closed, WHOLE EXECUTION: a failure that cannot be attributed to one file --
        geid = "99999999-9999-4999-8999-999999999999"
        gjob = _fake_job(root, geid)
        for n in range(3):
            _write(os.path.join(gjob.dirs.artifacts, f"g{n}.csv"), b"PLAINTEXT-%d\n" % n)
        s._retention[geid] = [time.monotonic() + 900, 0]

        def unlocalisable(*a, **kw):
            # What "cannot be attributed to one file" means: the directory would not open, the
            # entry bound was exceeded, libcrypto went away. Nothing on disk has been examined,
            # so nothing on disk can be trusted.
            raise sup.ArtifactCryptoError("simulated non-localisable failure")

        sup.seal_retained_artifacts = unlocalisable
        try:
            gsealed, gomitted_n, gsecured = s._seal_retained(gjob)
        finally:
            sup.seal_retained_artifacts = real_seal_retained_artifacts
        gremaining = sorted(os.listdir(gjob.dirs.artifacts))
        gclear = []
        for name in gremaining:
            with open(os.path.join(gjob.dirs.artifacts, name), "rb") as fh:
                if b"PLAINTEXT-" in fh.read():
                    gclear.append(name)
        check("artifact seal: FAIL CLOSED — a failure the pass cannot attribute to any one "
              "file destroys the whole directory, because nothing in it has been examined",
              not gclear, f"still in the clear: {gclear}" + seal_suffix + purge_suffix)
        check("artifact seal: FAIL CLOSED — and it does not vanish silently either: the "
              "destroyed artifacts are counted into artifacts_omitted and nothing is listed",
              gsealed == {} and gomitted_n == 3,
              f"got {gsealed!r} {gomitted_n}" + seal_suffix + purge_suffix)
        check("artifact seal: FAIL CLOSED — the whole-directory purge succeeded, so the pass "
              "reports the directory secured",
              gsecured is True, f"secured {gsecured!r}" + seal_suffix + purge_suffix)
        gentries, _, gdigests = sup.build_manifest(gjob.dirs.artifacts, sealed=gsealed)
        check("artifact seal: FAIL CLOSED — the manifest built afterwards advertises nothing, "
              "so no caller is told about an artifact it cannot have",
              gentries == [] and gdigests == {},
              f"got {gentries} {gdigests}" + seal_suffix + purge_suffix)
        check("artifact seal: FAIL CLOSED — and the execution still answers, because the "
              "retention path failing is not the script failing",
              geid in s._retention and geid not in s._artifact_keys,
              f"retained {geid in s._retention}, key {geid in s._artifact_keys}")

        # -- NOT fail-closed, and it says so: plaintext that could not be REMOVED either -----
        # A same-uid peer chmod 0500 on artifacts/ between the retain and the seal produced
        # "destroyed 0 rather than retaining them in the clear" over two files that were, at
        # that moment, in the clear. A count cannot distinguish "destroyed everything" from
        # "destroyed nothing", so _purge_artifacts returns whether the directory is empty.
        ueid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        ujob = _fake_job(root, ueid)
        for n in range(2):
            _write(os.path.join(ujob.dirs.artifacts, f"u{n}.csv"), b"PLAINTEXT-%d\n" % n)
        s._retention[ueid] = [time.monotonic() + 900, 0]
        sup.seal_retained_artifacts = unlocalisable
        sup._purge_artifacts = lambda d: (0, False)   # the chmod 0500 outcome, deterministically
        try:
            with _LogCapture() as ulog:
                usealed, uomitted_n, usecured = s._seal_retained(ujob)
        finally:
            sup.seal_retained_artifacts = real_seal_retained_artifacts
            sup._purge_artifacts = (_purge_that_keeps_plaintext if keeps_plaintext
                                    else real_purge)
        check("artifact seal: NOT SECURED — when the plaintext can be neither sealed nor "
              "deleted the pass says so, because that is the one outcome artifacts_omitted "
              "cannot describe",
              usecured is False and usealed == {},
              f"secured {usecured!r}, sealed {usealed!r}")
        check("artifact seal: NOT SECURED — and the log does not claim a property the code "
              "did not achieve: it says the artifacts are retained in the clear, and never "
              "'destroyed N rather than retaining them in the clear'",
              "RETAINED IN THE CLEAR" in "\n".join(ulog.lines)
              and "rather than retaining them in the clear" not in "\n".join(ulog.lines),
              f"logged {ulog.lines!r}")
        real_purge(ujob.dirs.artifacts)

        # -- an entry that will not even stat is removed and counted, not skipped ------------
        # `continue` on the os.stat left such an entry outside both halves of "what is not
        # sealed is deleted" and outside artifacts_omitted. A dangling symlink is the cheapest
        # way to build one whose stat succeeds only with follow_symlinks=False.
        seid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        sjob = _fake_job(root, seid)
        _write(os.path.join(sjob.dirs.artifacts, "kept.csv"), b"KEPT\n")
        _write(os.path.join(sjob.dirs.artifacts, "unstattable.csv"), b"PLAINTEXT-X\n")
        s._retention[seid] = [time.monotonic() + 900, 0]
        real_stat = os.stat

        def refusing_stat(path, *a, **kw):
            if path == "unstattable.csv":
                raise OSError(5, "simulated EIO")
            return real_stat(path, *a, **kw)

        os.stat = refusing_stat
        try:
            ssealed, somitted_n, ssecured = s._seal_retained(sjob)
        finally:
            os.stat = real_stat
        sleft = sorted(os.listdir(sjob.dirs.artifacts))
        unstat_name = ("artifact seal: an entry that cannot be examined is REMOVED and "
                       "COUNTED rather than skipped — nothing may be neither sealed, nor "
                       "purged, nor reported")
        if no_seal:
            skip(unstat_name, "SUPERVISOR_TEST_NO_SEAL=1 never examines an entry at all")
        else:
            check(unstat_name,
                  sleft == ["kept.csv"] and somitted_n == 1 and ssecured is True,
                  f"left {sleft}, omitted {somitted_n}, secured {ssecured!r}")

        # -- STRUCTURAL: a directory retained WITHOUT the seal pass is emptied ---------------
        # The original demonstrated attack, reproduced against the sealed build. _seal_retained
        # runs on the completion path only, so any exception out of _execute_inner propagated
        # past it and run()'s finally retained the directory with the child's plaintext where it
        # wrote it. The read path answering 404 is not a defence: the threat is a same-uid
        # open() on a flat, enumerable /scratch.
        reid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        rjob = sup.Job(types.SimpleNamespace(execution_id=reid), None)
        rjob.dirs = sup.ExecutionDirs(root, reid)
        rjob.dirs.create()
        _write(os.path.join(rjob.dirs.artifacts, "private.csv"), b"SECRET-VICTIM-DATA,1\n")
        _write(os.path.join(rjob.dirs.tmp, "scratch.tmp"), b"SECRET-VICTIM-DATA,2\n")
        check("artifact seal: (setup) a job that never reached the seal pass is not marked "
              "sealed", rjob.sealed is False, f"sealed {rjob.sealed!r}")
        s._release(rjob, retain=True)
        rleft = []
        for dirpath, _, filenames in os.walk(rjob.dirs.base):
            rleft.extend(os.path.join(dirpath, f) for f in filenames)
        rclear = []
        for path in rleft:
            with open(path, "rb") as fh:
                if b"SECRET-VICTIM-DATA" in fh.read():
                    rclear.append(os.path.relpath(path, rjob.dirs.base))
        check("artifact seal: STRUCTURAL — an execution that RAISED before the seal retains "
              "nothing in the clear; nothing was ever advertised for it, so emptying the "
              "directory costs no caller anything",
              not rclear, f"still in the clear: {sorted(rclear)}" + release_suffix)
        check("artifact seal: STRUCTURAL — and the directory itself stays, so the execution "
              "id remains reserved and the reaper removes it on the usual schedule",
              os.path.isdir(rjob.dirs.base) and reid in s._retention,
              f"dir {os.path.isdir(rjob.dirs.base)}, retained {reid in s._retention}")
        s._forget_retained(reid)

        # -- the startup gate ---------------------------------------------------------------
        probe_dir = os.path.join(tmp, "selftest")
        os.makedirs(probe_dir)
        selftest_raised = None
        try:
            sup.crypto_selftest(probe_dir)
        except Exception as exc:
            selftest_raised = exc
        check("artifact seal: crypto_selftest passes and leaves nothing behind — it is the "
              "startup gate, so a pod whose libcrypto cannot seal never reports ready",
              selftest_raised is None and os.listdir(probe_dir) == [],
              f"raised {selftest_raised!r}, left {os.listdir(probe_dir)}")
    finally:
        sup.Supervisor._seal_retained = real_seal_retained
        sup.Supervisor._secure_unsealed = real_secure_unsealed
        sup.artifact_aad = real_aad
        sup._purge_artifacts = real_purge
        sup.seal_artifact = real_seal_artifact
        sup.seal_retained_artifacts = real_seal_retained_artifacts


def test_artifact_scoping(tmp):
    """The id is the authorisation, and only a RETAINED execution has one."""
    root = os.path.join(tmp, "scoping")
    os.makedirs(root)
    eid = "11111111-1111-4111-8111-111111111111"
    dirs = sup.ExecutionDirs(root, eid)
    dirs.create()
    with open(os.path.join(dirs.artifacts, "plot.png"), "wb") as fh:
        fh.write(b"\x89PNG")

    s = sup.Supervisor(root, ready=True)
    # What a completed execution leaves behind: a retention row and the digest map its manifest
    # was built from. Both are what _retain and _execute_inner write; see test_artifact_integrity
    # for what the map is FOR.
    s._retention[eid] = [time.monotonic() + 900, 0]
    s._record_digests(eid, sup.build_manifest(dirs.artifacts)[2])
    expect_request_error("artifact scoping: a directory that exists but is not retained is 404",
                         lambda: s.read_artifact(eid, "plot.png"), 404, "NotFound")
    expect_request_error("artifact scoping: a malformed execution id is 400",
                         lambda: s.read_artifact("../other", "plot.png"), 400, "InvalidRequest")

    s._retained_ids.add(eid)
    data, ctype = s.read_artifact(eid, "plot.png")
    check("artifact scoping: a retained execution serves its artifact",
          data == b"\x89PNG" and ctype == "image/png", f"got {data!r} {ctype}")
    expect_request_error("artifact scoping: retention does not widen the name rules",
                         lambda: s.read_artifact(eid, "../plot.png"), 400, "InvalidRequest")


def test_artifact_fifo_does_not_block(tmp):
    """A listed name replaced by a FIFO must not hang the read.

    The hazard is a replacement during retention, not a plant: a planted fifo is not in the
    digest map and is refused before it is opened. But a same-uid peer can unlink a name the
    manifest DID list and mkfifo it back, and O_RDONLY on a writerless fifo blocks in the kernel
    before the S_ISREG that would refuse it ever runs — a one-line denial of service from inside
    the sandbox. This became the only read path when read_artifact stopped reading
    chat-backend's own filesystem.
    """
    root = os.path.join(tmp, "fifo")
    os.makedirs(root)
    eid = "44444444-4444-4444-8444-444444444444"
    dirs = sup.ExecutionDirs(root, eid)
    dirs.create()
    with open(os.path.join(dirs.artifacts, "results.tsv"), "wb") as fh:
        fh.write(b"rsid\tpval\n")

    s = sup.Supervisor(root, ready=True)
    s._retention[eid] = [time.monotonic() + 900, 0]
    s._record_digests(eid, sup.build_manifest(dirs.artifacts)[2])
    s._retained_ids.add(eid)
    data, _ = s.read_artifact(eid, "results.tsv")
    check("artifact fifo: the regular file it replaces is served normally",
          data == b"rsid\tpval\n", f"got {data!r}")

    os.unlink(os.path.join(dirs.artifacts, "results.tsv"))
    os.mkfifo(os.path.join(dirs.artifacts, "results.tsv"))

    outcome = {}

    def read():
        try:
            outcome["data"] = s.read_artifact(eid, "results.tsv")
        except BaseException as exc:
            outcome["exc"] = exc

    thread = threading.Thread(target=read, daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(10)
    elapsed = time.monotonic() - started
    check("artifact fifo: a listed name replaced by a FIFO does not block the read",
          not thread.is_alive(), f"still running after {elapsed:.1f}s")
    if thread.is_alive():
        return
    exc = outcome.get("exc")
    check("artifact fifo: it is refused as not-found, the same answer every other "
          "non-regular file gets",
          isinstance(exc, sup.RequestError) and exc.status == 404,
          f"got {outcome!r}")
    check("artifact fifo: and it is refused in well under a second, not at some timeout",
          elapsed < 1.0, f"took {elapsed:.1f}s")

    # _artifact_digest carries the same flag for the same reason: build_manifest stats an entry
    # and finds a regular file, and the replacement can land before the digest's own open.
    dfd = os.open(dirs.artifacts, os.O_RDONLY | os.O_DIRECTORY)
    try:
        digest = {}

        def hash_it():
            digest["value"] = sup._artifact_digest(dfd, "results.tsv")

        thread = threading.Thread(target=hash_it, daemon=True)
        thread.start()
        thread.join(10)
        # It returns a digest rather than None: a non-blocking read of a writerless fifo gives
        # EOF, not EAGAIN, so the hash is over zero bytes — which would match an empty regular
        # file swapped in before the read. What makes that moot is narrower: build_manifest's
        # `sealed is None` branch has no production caller, so this runs only here.
        check("artifact fifo: hashing one for the manifest does not block either",
              not thread.is_alive(), f"alive={thread.is_alive()} digest={digest!r}")
    finally:
        os.close(dfd)


def test_seal_fifo_does_not_block(tmp):
    """The seal pass must not hang on a FIFO either.

    This is the site production actually reaches: _artifact_digest runs only in build_manifest's
    `sealed is None` branch, which _execute_inner never takes, while seal_artifact's open runs on
    every completed execution. seal_retained_artifacts lstats the entry and then opens it by
    name — the identical check-then-open window, on the completion path, holding the execution
    slot with no timeout above it.
    """
    root = os.path.join(tmp, "sealfifo")
    os.makedirs(root)
    eid = "45454545-4545-4545-8545-454545454545"
    dirs = sup.ExecutionDirs(root, eid)
    dirs.create()
    with open(os.path.join(dirs.artifacts, "keep.tsv"), "wb") as fh:
        fh.write(b"rsid\tpval\n")
    os.mkfifo(os.path.join(dirs.artifacts, "results.tsv"))

    key = bytearray(os.urandom(sup.ARTIFACT_KEY_BYTES))
    outcome = {}

    def seal():
        try:
            outcome["value"] = sup.seal_retained_artifacts(dirs.artifacts, eid, key)
        except BaseException as exc:  # noqa: BLE001 - reported through the check below
            outcome["exc"] = exc

    thread = threading.Thread(target=seal, daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(10)
    elapsed = time.monotonic() - started
    check("seal fifo: a listed name replaced by a FIFO does not block the seal pass",
          not thread.is_alive(), f"still running after {elapsed:.1f}s")
    if thread.is_alive():
        return
    check("seal fifo: and it returns in well under a second, not at some timeout",
          elapsed < 1.0, f"took {elapsed:.1f}s")
    check("seal fifo: the pass completed rather than raising",
          "exc" not in outcome, f"raised {outcome.get('exc')!r}")
    if "exc" in outcome:
        return
    sealed, _purged, _growth, stranded = outcome["value"]
    # seal_retained_artifacts stats before the open, so here the fifo is rejected at S_ISREG and
    # never reaches seal_artifact. The point is the deadline; the open itself is driven below.
    check("seal fifo: the real artifact still sealed",
          "keep.tsv" in sealed and stranded == 0, f"got {sealed!r} stranded={stranded}")

    # THE WINDOW ITSELF: seal_artifact called on a name that is a fifo, which is exactly what
    # seal_retained_artifacts holds after a peer swaps the file between its stat and this open.
    # Re-made here because the pass above already purged the first one at its S_ISREG check.
    os.mkfifo(os.path.join(dirs.artifacts, "results.tsv"))
    dfd = os.open(dirs.artifacts, os.O_RDONLY | os.O_DIRECTORY)
    try:
        direct = {}

        def seal_one():
            try:
                direct["value"] = sup.seal_artifact(dfd, "results.tsv", key,
                                                    sup.artifact_aad(eid, "results.tsv"))
            except BaseException as exc:  # noqa: BLE001 - reported through the check below
                direct["exc"] = exc

        thread = threading.Thread(target=seal_one, daemon=True)
        started = time.monotonic()
        thread.start()
        thread.join(10)
        elapsed = time.monotonic() - started
        check("seal fifo: seal_artifact itself does not block on a FIFO in that window",
              not thread.is_alive(), f"still running after {elapsed:.1f}s")
        if thread.is_alive():
            return
        check("seal fifo: it returns in well under a second",
              elapsed < 1.0, f"took {elapsed:.1f}s")
        # EOF on the first read, so what is renamed over the name is an empty sealed regular
        # file. Intended, not a hole: a peer able to swap the name could have truncated it
        # anyway, and the digest recorded is the digest of what will be served.
        check("seal fifo: the fifo is replaced by an empty sealed regular file",
              direct.get("value") == (0, hashlib.sha256(b"").hexdigest()),
              f"got {direct!r}")
        st = os.stat("results.tsv", dir_fd=dfd, follow_symlinks=False)
        check("seal fifo: and the name is no longer a fifo",
              sup.stat.S_ISREG(st.st_mode), f"mode={st.st_mode:o}")
    finally:
        os.close(dfd)
