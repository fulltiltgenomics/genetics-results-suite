import json

from .audit import PROBE
from .harness import check, make_body


def test_container(server):
    """Everything here needs the image: the read-only rootfs, the pruned venv, the baked
    font cache and the absence of credentials are all shipped properties."""
    status, _, body = server.request("POST", "/execute", body=make_body(code=PROBE))
    if status != 200 or body.get("status") != "ok":
        check("container: probe script ran", False, f"got {status} {body}")
        return
    probe = json.loads(body["output"].split("PROBE ", 1)[1].splitlines()[0])
    env = probe["env"]

    check("container: child runs as uid 65532, the SUPERVISOR's uid",
          probe["uid"] == 65532 and probe["gid"] == 65532, f"got {probe['uid']}:{probe['gid']}")
    # SANDBOX_CHILD_UID=65533 is advertised by the image and names a uid nothing can switch
    # to: option (a) needs CAP_SETUID and drop-ALL leaves none (section 2, "The uid choice").
    check("container: the child did NOT become the advertised 65533", probe["uid"] != 65533)

    check("container: root filesystem is read-only", probe["write_rootfs"] == 30,
          f"errno {probe['write_rootfs']}")
    check("container: there is no writable /tmp", probe["write_tmp"] is not None,
          f"errno {probe['write_tmp']}")
    check("container: TMPDIR is writable and under /scratch",
          probe["write_tmpdir"] is None and env["TMPDIR"].startswith("/scratch/"),
          f"{env['TMPDIR']} errno {probe['write_tmpdir']}")
    check("container: cwd is the per-execution tmp, not artifacts/",
          probe["cwd"].startswith("/scratch/") and probe["cwd"].endswith("/tmp"), probe["cwd"])

    # prune_venv.py deletes pip and everything outside the SDK's import closure. Asserted at
    # build time by build-checks.py; asserted here from INSIDE a real execution, which is the
    # position an attacker actually occupies.
    check("container: no package manager (pip)", probe["import_pip"] is False)
    check("container: no setuptools", probe["import_setuptools"] is False)
    check("container: no google-auth (section 3(c))", probe["import_google_auth"] is False)
    check("container: the genetics SDK imports", probe["import_genetics_mcp_server_sdk"] is True)
    check("container: matplotlib imports under the read-only rootfs",
          probe["import_matplotlib"] is True)

    check("container: no credential is in the child's environment",
          env["INTERNAL_API_SECRET"] is None and env["SANDBOX_TOKEN_SIGNING_KEY"] is None,
          f"got {env}")
    check("container: the image's env markers are set, so degradations are off",
          env["GENETICS_PREWARM"] and env["GENETICS_MPLCACHE"], f"got {env}")
    check("container: SANDBOX_SCRATCH_ROOT is unset, so /scratch is the real /scratch",
          env["SANDBOX_SCRATCH_ROOT"] is None, f"got {env['SANDBOX_SCRATCH_ROOT']!r}")
    check("container: the two API endpoints are configured and are not cluster FQDNs",
          env["GENETICS_API_URL"] and env["BIGQUERY_API_URL"]
          and "svc.cluster.local" not in (env["GENETICS_API_URL"] + env["BIGQUERY_API_URL"]),
          f"got {env['GENETICS_API_URL']} {env['BIGQUERY_API_URL']}")

    # The font cache is baked at build time and copied into the per-execution MPLCONFIGDIR
    # because the rootfs is read-only. On matplotlib 3.10 an unwritable MPLCONFIGDIR raises
    # rather than falling back, so "it imports" is not enough: the plot has to be produced and
    # land in the manifest.
    plot = (
        "import os, matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "plt.plot([1, 2, 3], [3, 1, 2])\n"
        "plt.savefig(os.path.join(os.environ['SANDBOX_ARTIFACTS_DIR'], 'plot.png'))\n"
        "print('MPLCONFIGDIR', os.environ['MPLCONFIGDIR'])\n"
    )
    status, _, body = server.request("POST", "/execute", body=make_body(code=plot))
    check("container: a plot is written under the read-only rootfs",
          status == 200 and body["status"] == "ok", f"got {status} {body.get('error')}")
    check("container: the plot reaches the artifact manifest",
          [e["name"] for e in body.get("artifacts", [])] == ["plot.png"],
          f"got {body.get('artifacts')}")
    check("container: the plot's size is non-zero",
          body.get("artifacts") and body["artifacts"][0]["size"] > 0,
          f"got {body.get('artifacts')}")
