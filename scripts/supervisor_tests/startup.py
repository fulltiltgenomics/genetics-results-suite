import os

from .harness import check, sup


def test_startup_wipe(tmp):
    root = os.path.join(tmp, "wipe-root")
    os.makedirs(os.path.join(root, "11111111-1111-4111-8111-111111111111", "artifacts"))
    with open(os.path.join(root, "11111111-1111-4111-8111-111111111111", "artifacts", "x"), "w") as fh:
        fh.write("secret")
    os.makedirs(os.path.join(root, sup.SUPERVISOR_DIR_NAME))
    with open(os.path.join(root, "stray-file"), "w") as fh:
        fh.write("x")

    removed = sup.wipe_unrecognised_scratch(root)
    check("wipe: removes an orphaned execution directory",
          "11111111-1111-4111-8111-111111111111" in removed)
    check("wipe: removes stray files too", "stray-file" in removed)
    check("wipe: keeps the supervisor's own directory",
          os.path.isdir(os.path.join(root, sup.SUPERVISOR_DIR_NAME)))
    check("wipe: nothing readable is left behind", os.listdir(root) == [sup.SUPERVISOR_DIR_NAME])
