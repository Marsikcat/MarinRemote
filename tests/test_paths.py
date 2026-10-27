"""
Paths a viewer sends: nothing leaves the allowed folders — not by "..", not by drive letters,
UNC or \\\\?\\ paths, device names or alternate streams, and not through a junction or a
symbolic link that leads outside, even one made after the path was checked.
"""
import os
import subprocess

import pytest

from marinremote.host.fs import FsError, Roots, check_name, inside, split_virtual


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "allowed"
    outside = tmp_path / "secret"
    (root / "docs" / "sub").mkdir(parents=True)
    outside.mkdir()
    (root / "docs" / "a.txt").write_text("a")
    (outside / "passwords.txt").write_text("nope")
    roots = Roots([{"id": "home", "path": str(root), "label": "Дом"}])
    return roots, root, outside


@pytest.mark.parametrize("bad", [
    "", "/home/docs", "\\home", "C:/Windows", "home/../secret", "home/docs/../../secret",
    "home/./docs", "home//docs", "home/docs/a.txt:stream", "home/C:", "home/con", "home/NUL.txt",
    "home/com1", "home/lpt9.log", "home/docs ", "home/docs.", "home/a*b", "home/a?b", 'home/a"b',
    "home/a|b", "home/a<b", "home/a\x00b", "\\\\server\\share\\x", "//server/share/x",
    "\\\\?\\C:\\Windows", "home\\..\\secret", "nosuchroot/x", "x" * 5000,
])
def test_bad_paths_are_refused_by_their_text(tree, bad):
    roots, _, _ = tree
    with pytest.raises(FsError) as e:
        roots.resolve(bad)
    assert e.value.code in ("bad_path", "not_found")


def test_good_paths(tree):
    roots, root, _ = tree
    assert split_virtual("home/docs/a.txt") == ("home", ["docs", "a.txt"])
    assert split_virtual("home\\docs\\") == ("home", ["docs"])
    assert os.path.samefile(roots.resolve("home/docs/a.txt"), root / "docs" / "a.txt")
    assert os.path.samefile(roots.resolve("home"), root)
    names = {e["n"]: e for e in roots.list("home/docs")}
    assert names["a.txt"]["s"] == 1 and names["sub"]["d"]
    assert roots.listing() == [{"id": "home", "label": "Дом"}]


def test_names():
    for ok in ("a.txt", "Отчёт 2026.docx", ".hidden", "a b", "x" * 255):
        assert check_name(ok) == ok
    for bad in ("", ".", "..", "a/b", "a\\b", "a:b", "CON", "aux.txt", "a ", "a.", "x" * 256):
        with pytest.raises(FsError):
            check_name(bad)


def test_inside_is_case_insensitive_and_exact():
    assert inside(r"C:\Users\Me\Docs", r"c:\users\me")
    assert inside(r"C:\Users\Me", "C:\\Users\\Me\\")
    assert not inside(r"C:\Users\Megan", r"C:\Users\Me")       # a prefix is not a parent
    assert not inside(r"C:\Users", r"C:\Users\Me")


def junction(link, target):
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    if r.returncode != 0:
        pytest.skip("can't make a junction here")


def test_a_junction_out_of_the_root_is_refused(tree):
    roots, root, outside = tree
    junction(root / "docs" / "escape", outside)
    listed = {e["n"]: e for e in roots.list("home/docs")}
    assert listed["escape"].get("l")                          # shown, marked as a link
    for path in ("home/docs/escape", "home/docs/escape/passwords.txt"):
        with pytest.raises(FsError) as e:
            roots.resolve(path)
        assert e.value.code == "bad_path"
    with pytest.raises(FsError):
        roots.open_read("home/docs/escape/passwords.txt")
    with pytest.raises(FsError):
        roots.resolve_new("home/docs/escape/new.txt")         # nor writing through it
    with pytest.raises(FsError):
        roots.list("home/docs/escape")


def test_a_junction_inside_the_root_is_fine(tree):
    roots, root, _ = tree
    junction(root / "shortcut", root / "docs")
    assert roots.list("home/shortcut")


def test_a_symbolic_link_out_of_the_root_is_refused(tree):
    roots, root, outside = tree
    try:
        os.symlink(outside / "passwords.txt", root / "docs" / "link.txt")
    except OSError:
        pytest.skip("symbolic links need Developer Mode or admin rights here")
    with pytest.raises(FsError) as e:
        roots.open_read("home/docs/link.txt")
    assert e.value.code == "bad_path"


def test_swapping_a_folder_for_a_junction_after_the_check_does_not_help(tree):
    """The check is on the opened handle: what is written is what was checked."""
    roots, root, outside = tree
    folder, name, r = roots.resolve_new("home/docs/sub/new.txt")   # checked while it was a folder
    os.rmdir(root / "docs" / "sub")
    junction(root / "docs" / "sub", outside)                       # …and now it leads out
    with pytest.raises(FsError) as e:
        roots.open_write(folder, name, r)
    assert e.value.code == "bad_path"
    assert not (outside / "new.txt").exists() or (outside / "new.txt").stat().st_size == 0


def test_folder_operations(tree):
    roots, root, _ = tree
    roots.mkdir("home/docs/new")
    roots.mkdir("home/docs/new")                               # already there: fine
    assert (root / "docs" / "new").is_dir()
    roots.rename("home/docs/a.txt", "b.txt")
    assert (root / "docs" / "b.txt").exists()
    with pytest.raises(FsError):
        roots.rename("home/docs/b.txt", "../b.txt")
    with pytest.raises(FsError):
        roots.rename("home", "x")
    with pytest.raises(FsError):
        roots.delete("home")
    with pytest.raises(FsError) as e:
        roots.mkdir("home/docs/b.txt")                        # a file is in the way
    assert e.value.code == "exists"


def test_a_missing_root_is_simply_not_offered(tmp_path):
    roots = Roots([{"id": "gone", "path": str(tmp_path / "nope"), "label": "x"},
                   {"id": "here", "path": str(tmp_path), "label": "y"}])
    assert [r["id"] for r in roots.listing()] == ["here"]


def test_every_drive_as_a_root():
    from marinremote.host.fs import drive_roots
    system = os.environ.get("SystemDrive", "C:")[0].upper()
    drives = drive_roots()
    assert system in [d["id"] for d in drives]
    roots = Roots(drives)
    assert os.path.normcase(roots.resolve(f"{system}/Windows")) == os.path.normcase(os.environ["SystemRoot"])
    for bad in (f"{system}/..", f"{system}:/Windows", f"{system}/Windows/../..", system + "\\..\\" + system):
        with pytest.raises(FsError):
            roots.resolve(bad)


def test_files_offer_every_drive_only_when_allowed(tmp_path):
    from types import SimpleNamespace
    from marinremote.host.files import FilesService
    s = {"roots": [{"id": "home", "path": str(tmp_path), "label": "Дом"}], "files_all_drives": False}
    svc = FilesService(SimpleNamespace(net=None, s=s))
    assert [r["id"] for r in svc.roots().listing()] == ["home"]
    s["files_all_drives"] = True
    ids = [r["id"] for r in svc.roots().listing()]
    assert ids[0] == "home" and os.environ.get("SystemDrive", "C:")[0].upper() in ids
    s["files_all_drives"] = False
    assert [r["id"] for r in svc.roots().listing()] == ["home"]
