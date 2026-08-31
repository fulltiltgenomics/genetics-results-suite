#!/usr/bin/env python3
"""Offline assertions about scripts/gen-sandbox-docs.py and what it produces.

Run: python3 scripts/test-sandbox-docs.py [--sdk-src DIR]
     (--sdk-src defaults to whatever gen-sandbox-docs.py resolves — never the
      staged sandbox/.sdk-src)
Exit 0 = pass, 1 = a property is broken, 2 = the harness could not run.

The property under test is not "the generator runs" — it is that the sandbox image's
schema documentation is DERIVED from configs/datasets.yaml rather than transcribed from
it. That distinction has no runtime symptom: a hardcoded rule produces a perfectly
healthy image whose /genetics/schema contradicts the canonical file, and the model
believes the image. So three things are checked separately:

  1. COVERAGE — every view in datasets.yaml gets a file, with every column and every
     worked example in it. A regex-based view list that failed open is exactly how a view
     goes missing, so the list here comes from yaml.safe_load, never from a pattern.
  2. THE RULES ARE IN THE CANONICAL FILE — the hard-won correctness rules named by
     the generator needs are asserted to be present *in datasets.yaml*, in a
     named field. If someone deletes the peak_to_gene warning from the YAML, this fails
     here rather than silently shipping an image that no longer carries it.
  3. THE GENERATOR DOES NOT KNOW THEM — the same rule text must NOT appear in
     gen-sandbox-docs.py, and mutating the YAML must move the generated output. Together
     these are what "generated, never transcribed" means operationally.

Why (3) matters right now rather than in principle: there is open
P1 that rewrites credible_sets_v's variant/chr guidance as soon as the clustering swap
runs. A transcribed copy would keep shipping the old rule and nothing would report it.
"""

import argparse
import ast
import copy
import importlib.util
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GENERATOR = os.path.join(ROOT, "scripts", "gen-sandbox-docs.py")
DATASETS_YAML = os.path.join(ROOT, "configs", "datasets.yaml")
SCHEMA_DIR = os.path.join(ROOT, "sandbox", "schema")
STUBS_DIR = os.path.join(ROOT, "sandbox", "stubs")

try:
    import yaml
except ImportError:
    print("harness cannot run: PyYAML is missing (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_sandbox_docs", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The correctness rules, each located by the datasets.yaml
# field that must carry it and a phrase that must survive in that field. These strings are
# deliberately here and NOT in the generator: this file is the tripwire, the generator is
# the transport. A rule reworded in datasets.yaml fails here loudly, which is a request to
# re-read the new wording and update the marker — not to copy the rule into the generator.
RULES = [
    {
        "name": "peak_to_gene join warning",
        "table": "credible_sets_v",
        "field": ("description",),
        "markers": ["peak_to_gene_v", "peak_id = trait", "1 Mb"],
        "docs": ["credible_sets_v.md"],
    },
    {
        "name": "peak_to_gene join warning (from the other side)",
        "table": "peak_to_gene_v",
        "field": ("description",),
        "markers": ["peak_id = trait", "1 Mb"],
        "docs": ["peak_to_gene_v.md"],
    },
    {
        "name": "chr alongside variant for partition pruning",
        "table": "credible_sets_v",
        "field": ("examples",),
        "markers": ["partitioned by chr", "bytes-billed"],
        "docs": ["credible_sets_v.md"],
    },
    {
        "name": "credible-set key is not cs_id alone",
        "table": "credible_sets_v",
        "field": ("columns", "cs_id"),
        "markers": [
            "(resource, dataset, trait, cell_type, cs_id)",
            "NOT unique on its own",
            # a plain equi-join on the key drops every GWAS row, cell_type being NULL there
            "IS NOT DISTINCT FROM",
        ],
        "docs": ["credible_sets_v.md"],
    },
    {
        # the only rule here whose absence returns a confident WRONG answer rather than an
        # error: (dataset, cs_id) matches every trait fine-mapped in the same region
        "name": "coloc_credsets_v join carries trait and cell type",
        "table": "coloc_credsets_v",
        "field": ("description",),
        "markers": ["trait_original", "IS NOT DISTINCT FROM", "is NOT a key"],
        "docs": ["coloc_credsets_v.md"],
    },
]


def _generator_code():
    """The generator with comments and docstrings removed.

    The anti-transcription checks below have to distinguish CODE that knows a view name
    from PROSE that explains why it must not. gen-sandbox-docs.py's own commentary names
    credible_sets_v and the SDK functions on purpose — deleting that explanation to keep a
    naive substring check green would be the wrong trade, so strip the parts that cannot
    affect the output and assert on the rest.
    """
    tree = ast.parse(open(GENERATOR).read())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            body.pop(0)
            if not body:
                body.append(ast.Pass())
    return ast.unparse(tree)


_SCALAR_TYPES = {
    "BIGNUMERIC", "BOOL", "BYTES", "DATE", "DATETIME", "FLOAT64", "GEOGRAPHY", "INT64",
    "INTERVAL", "JSON", "NUMERIC", "STRING", "TIME", "TIMESTAMP",
}


def _valid_bq_type(text):
    """Is `text` spelled the way genetics_results.INFORMATION_SCHEMA.COLUMNS.data_type
    spells a type?

    Presence alone is not enough to be worth shipping. `column_types` is hand-copied out
    of a query result, and 'INT', 'float', 'array of int' or a pasted description are all
    things that would satisfy a non-empty check while telling the model something false —
    which is strictly worse than the missing type this field was added to fix. A grammar
    check is the strongest thing available offline; see the note on _column_types.
    """
    if not isinstance(text, str):
        return False
    text = text.strip()
    if text in _SCALAR_TYPES:
        return True
    for wrapper in ("ARRAY", "RANGE"):
        prefix = f"{wrapper}<"
        if text.startswith(prefix) and text.endswith(">"):
            return _valid_bq_type(text[len(prefix):-1])
    if text.startswith("STRUCT<") and text.endswith(">"):
        depth = 0
        fields, current = [], ""
        for char in text[len("STRUCT<"):-1]:
            if char == "<":
                depth += 1
            elif char == ">":
                depth -= 1
            if char == "," and depth == 0:
                fields.append(current)
                current = ""
            else:
                current += char
        fields.append(current)
        for field in fields:
            parts = field.strip().split(None, 1)
            if not parts or not _valid_bq_type(parts[-1]):
                return False
        return True
    # STRING(20), NUMERIC(38, 9) etc.
    head, _, tail = text.partition("(")
    if tail.endswith(")") and head.strip() in _SCALAR_TYPES:
        return all(part.strip().isdigit() for part in tail[:-1].split(","))
    return False


def _render_mutated(gen, mutated):
    """Render a modified copy of the config through the real generator.

    Written to a temp file rather than passed in memory because render_schema_docs takes a
    path; the canonical configs/datasets.yaml is never touched.
    """
    path = os.path.join(os.environ.get("TMPDIR", "/tmp"), "gen-sandbox-docs-mutation.yaml")
    with open(path, "w") as fh:
        yaml.safe_dump(mutated, fh, allow_unicode=True)
    try:
        return gen.render_schema_docs(path)
    finally:
        os.remove(path)


def _field_text(table, field):
    """The YAML text a rule is supposed to live in. `examples` folds every example's
    description and SQL together, because which example carries a rule is an editorial
    choice this harness has no business pinning."""
    if field == ("examples",):
        return "\n".join(
            f"{e.get('description', '')}\n{e.get('sql', '')}" for e in table.get("examples") or []
        )
    node = table
    for key in field:
        node = node[key]
    return str(node)


failures = []


def check(name):
    def wrap(fn):
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as exc:
            print(f"  FAIL {name}: {exc}")
            failures.append(name)

    return wrap


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdk-src", default=None)
    args = parser.parse_args(argv)

    gen = _load_generator()
    with open(DATASETS_YAML) as fh:
        config = yaml.safe_load(fh)
    tables = config["tables"]
    generator_code = _generator_code()
    try:
        schema_files = gen.render_schema_docs(DATASETS_YAML)
    except SystemExit as exc:
        # the generator refuses to render an incomplete datasets.yaml (a column with no
        # column_types entry, an empty tables block). Report it as a named failure instead
        # of letting SystemExit escape as a bare message with no check name attached.
        print(f"  FAIL the generator can render configs/datasets.yaml: {exc}")
        print("sandbox docs checks: 1 failure(s)")
        return 1

    # resolved by the generator, so the harness and the thing it tests can never read
    # different SDKs — and neither falls back to the stale sandbox/.sdk-src a build left
    # behind
    sdk_src = gen.resolve_sdk_src(args.sdk_src)
    sdk_dir = sdk_src and gen.sdk_dir_for(sdk_src)
    if not sdk_dir or not os.path.isdir(sdk_dir):
        # exit 2, not a skip: four of the thirteen checks — including the only one that can
        # catch a stub documenting a function the SDK does not export — need the source, and
        # a green run that quietly covered nine of thirteen is how that gap ships. Same
        # convention as gen-sandbox-docs.py and scripts/test-network-policies.py.
        print(f"harness cannot run: {gen.sdk_src_error(sdk_src)}", file=sys.stderr)
        return 2
    if not args.sdk_src:
        print(f"SDK source: {sdk_src}", file=sys.stderr)

    @check("every view in datasets.yaml produces a schema file")
    def _coverage():
        """The one failure mode CLAUDE.md calls out by name: a view list that rots. The
        expected set is loaded from the YAML, so a view added there and forgotten here is
        impossible by construction; what this catches is the generator dropping one."""
        expected = {f"{name}.md" for name in tables}
        produced = set(schema_files) - {"README.md"}
        assert produced == expected, (
            f"missing {sorted(expected - produced)}, unexpected {sorted(produced - expected)}"
        )
        assert len(expected) >= 15, f"only {len(expected)} views parsed out of datasets.yaml"

    @check("every column, enumerable column and worked example reaches its file")
    def _completeness():
        """categorical_columns is checked here too, and not only that the column name
        appears: `columns` already puts every name in the file, so a render_view that
        dropped the whole enumerable block would still pass on names alone. The scoping
        parent is what carries the information — 'the values of dataset are scoped by
        resource' is the difference between one SELECT DISTINCT and a wrong guess."""
        for name, table in tables.items():
            doc = schema_files[f"{name}.md"]
            for column in table["columns"]:
                assert f"`{column}`" in doc, f"{name}: column {column} missing"
            categorical = table.get("categorical_columns") or {}
            for column, parent in categorical.items():
                row = f"| `{column}` | {f'`{parent}`' if parent else '—'} |"
                assert row in doc, (
                    f"{name}: enumerable column {column} (scoped by {parent!r}) is not "
                    f"listed as {row!r}"
                )
            for example in table.get("examples") or []:
                first = str(example["sql"]).strip().splitlines()[0].strip()
                assert first in doc, f"{name}: example SQL missing ({first!r})"

    @check("every documented column carries a well-formed BigQuery type")
    def _column_types():
        """A column documented without its type is the
        defect this field exists to close — an agent writing SQL cannot tell an INT64
        chromosome from a string one, or an ARRAY that needs UNNEST from a scalar.

        Checked against configs/datasets.yaml, not the rendered doc, and in BOTH
        directions: a missing entry is a column the model gets no type for, an extra
        entry is a type for a column that no longer exists, which will be silently
        attached to the wrong thing the day that name comes back.

        WHY THE TYPE IS NOT COMPARED AGAINST LIVE BIGQUERY HERE. This harness gates
        scripts/build.sh on a build host with no BigQuery credentials, no network
        guarantee and no reason to grow either — every other check in it is a pure
        function of files in the repo. A live comparison would turn a docker build into
        something that fails when a service account rotates. The offline substitute is
        the grammar check: it cannot tell FLOAT64 from INT64, but it does reject
        everything that is not a type at all. Re-populating from
        `genetics_results.INFORMATION_SCHEMA.COLUMNS` is a step in
        docs/adding-datasets.md instead, where the person who changed a view is."""
        for name, table in tables.items():
            types = table.get("column_types")
            assert isinstance(types, dict) and types, (
                f"{name}: no `column_types:` block in configs/datasets.yaml. Populate it "
                "from genetics_results.INFORMATION_SCHEMA.COLUMNS (docs/adding-datasets.md)"
            )
            missing = [c for c in table["columns"] if c not in types]
            assert not missing, (
                f"{name}: no type for {missing} — every column in `columns:` needs one in "
                "`column_types:`"
            )
            extra = [c for c in types if c not in table["columns"]]
            assert not extra, (
                f"{name}: `column_types:` names {extra}, which `columns:` does not "
                "document — a stale type outlives the column it described"
            )
            malformed = {c: types[c] for c in table["columns"] if not _valid_bq_type(types[c])}
            assert not malformed, (
                f"{name}: not BigQuery type spellings: {malformed} — copy data_type "
                "verbatim from INFORMATION_SCHEMA.COLUMNS (e.g. INT64, ARRAY<STRING>)"
            )

    @check("the type of every column reaches its schema file")
    def _types_in_docs():
        """Names alone already appear in the file, so this asserts the whole rendered row
        prefix: a generator that emitted an empty type cell would pass a substring check
        on the column name and still show the model nothing."""
        for name, table in tables.items():
            doc = schema_files[f"{name}.md"]
            for column, type_name in table["column_types"].items():
                row = f"| `{column}` | `{type_name}` |"
                assert row in doc, f"{name}: {column} is not rendered with its type as {row!r}"

    @check("a type changed in datasets.yaml changes the generated output")
    def _type_mutation():
        view = sorted(tables)[0]
        column = next(iter(tables[view]["columns"]))
        mutated = copy.deepcopy(config)
        mutated["tables"][view]["column_types"][column] = "SENTINEL64"
        regenerated = _render_mutated(gen, mutated)
        assert f"| `{column}` | `SENTINEL64` |" in regenerated[f"{view}.md"], (
            f"changing tables.{view}.column_types.{column} did not change {view}.md — the "
            "generator is not reading the type from the canonical file"
        )

    @check("a column with no type is REFUSED, not rendered blank")
    def _missing_type_fails_closed():
        """The direction that matters. A guard that only proves the type shows up when it
        is present fails open: the failure mode is a column whose entry was never added,
        and a generator that quietly emitted `| chr |  |` would satisfy every other check
        in this file while shipping exactly the gap this check exists for. So delete an
        entry and require the generator to refuse."""
        view = sorted(tables)[0]
        column = next(iter(tables[view]["columns"]))
        for drop_whole_block in (False, True):
            mutated = copy.deepcopy(config)
            if drop_whole_block:
                del mutated["tables"][view]["column_types"]
            else:
                del mutated["tables"][view]["column_types"][column]
            try:
                _render_mutated(gen, mutated)
            except SystemExit:
                continue
            raise AssertionError(
                f"the generator rendered {view}.md with "
                + ("no column_types block" if drop_whole_block else f"no type for {column}")
                + " instead of refusing — the check fails open"
            )

    @check("the index lists every view")
    def _index():
        index = schema_files["README.md"]
        for name in tables:
            assert f"({name}.md)" in index, f"{name} absent from README.md"

    @check("the correctness rules are present in configs/datasets.yaml")
    def _rules_in_yaml():
        """Asserted against the canonical file, not the generated output: the generated
        output cannot be right if the source no longer says it."""
        for rule in RULES:
            text = _field_text(tables[rule["table"]], rule["field"])
            for marker in rule["markers"]:
                assert marker in text, (
                    f"{rule['name']}: {marker!r} is no longer in "
                    f"tables.{rule['table']}.{'.'.join(rule['field'])} — the rule left the "
                    "canonical file, or was reworded (update the marker here after reading it)"
                )

    @check("the correctness rules reach the right schema files")
    def _rules_in_docs():
        for rule in RULES:
            for doc_name in rule["docs"]:
                doc = schema_files[doc_name]
                for marker in rule["markers"]:
                    assert marker in doc, f"{rule['name']}: {marker!r} missing from {doc_name}"

    @check("the generator does not contain the rule text (generated, not transcribed)")
    def _not_hardcoded():
        """If any rule phrase appears in the generator, the rule has been copied and
        datasets.yaml is no longer the source. Also checks the table and column names, so
        a generator that special-cases credible_sets_v to append prose fails here too."""
        for rule in RULES:
            for marker in rule["markers"]:
                assert marker not in generator_code, (
                    f"{rule['name']}: {marker!r} is hardcoded in gen-sandbox-docs.py; it "
                    "must be read from configs/datasets.yaml at generation time"
                )
        for name in tables:
            assert name not in generator_code, (
                f"the generator names the view {name} — view-specific handling defeats "
                "the point; every view must be rendered by the same code path"
            )

    @check("a rule changed in datasets.yaml changes the generated output")
    def _mutation():
        """The positive half of the check above: absence of the text in the generator does
        not prove the generator reads it. Mutating the YAML must move the output."""
        for rule in RULES:
            mutated = copy.deepcopy(config)
            table = mutated["tables"][rule["table"]]
            sentinel = f"SENTINEL-{rule['table']}-{'-'.join(rule['field'])}"
            if rule["field"] == ("examples",):
                table["examples"][0]["description"] += " " + sentinel
            else:
                node = table
                for key in rule["field"][:-1]:
                    node = node[key]
                node[rule["field"][-1]] = str(node[rule["field"][-1]]) + " " + sentinel

            regenerated = _render_mutated(gen, mutated)
            doc_name = rule["docs"][0]
            assert sentinel in regenerated[doc_name], (
                f"{rule['name']}: mutating tables.{rule['table']}."
                f"{'.'.join(rule['field'])} did not change {doc_name} — the generator is "
                "not reading that field"
            )
            assert regenerated[doc_name] != schema_files[doc_name], "output identical after mutation"

    @check("no placeholders survive and neither directory is empty")
    def _placeholders():
        """The build gate: the build refuses while
        PLACEHOLDER files are staged, and an empty directory would satisfy that check
        while shipping nothing."""
        for directory, suffix in ((SCHEMA_DIR, ".md"), (STUBS_DIR, ".pyi")):
            assert os.path.isdir(directory), f"{directory} does not exist"
            entries = os.listdir(directory)
            assert not [e for e in entries if e.startswith("PLACEHOLDER")], (
                f"placeholders still in {directory}: {entries}"
            )
            assert [e for e in entries if e.endswith(suffix)], f"{directory} is empty"

    @check("the committed schema files match a fresh generation")
    def _schema_fresh():
        for name, content in schema_files.items():
            path = os.path.join(SCHEMA_DIR, name)
            assert os.path.exists(path), f"{name} not committed — run scripts/gen-sandbox-docs.py"
            assert open(path).read() == content, (
                f"{name} differs from a fresh generation — run scripts/gen-sandbox-docs.py"
            )

    stub_files = gen.render_stubs(sdk_dir)

    @check("stubs cover exactly the SDK's exported surface")
    def _stub_surface():
        """The image is pruned to the SDK's import closure
        and asserts EQUALITY with an allow-list. Documenting a function the package
        does not export, or missing one it does, is the same class of error.

        EQUALITY, not containment: a stub is the only description of the SDK the
        sandbox agent gets, so an extra name in it is a function the model will call
        and the interpreter will not find. The four names below are the ones the
        generator adds on purpose (module-level lifecycle helpers plus the region
        parser); anything else appearing here means the generator invented a name."""
        init_tree = ast.parse(open(os.path.join(sdk_dir, "__init__.py")).read())
        exported = gen._exported_functions(init_tree)
        allowed_extra = {"configure", "get_client", "close", "parse_region"}
        stub_tree = ast.parse(stub_files["genetics.pyi"])
        stubbed = {n.name for n in stub_tree.body if isinstance(n, ast.FunctionDef)}
        assert stubbed == set(exported) | allowed_extra, (
            f"not stubbed: {sorted(set(exported) - stubbed)}; "
            f"stubbed but not exported: {sorted(stubbed - set(exported) - allowed_extra)}"
        )
        client_tree = ast.parse(stub_files["client.pyi"])
        cls = next(n for n in client_tree.body if isinstance(n, ast.ClassDef))
        methods = {
            n.name
            for n in cls.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        source_methods = {
            n.name
            for n in next(
                c
                for c in ast.parse(open(os.path.join(sdk_dir, "client.py")).read()).body
                if isinstance(c, ast.ClassDef) and c.name == "GeneticsClient"
            ).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and (not n.name.startswith("_") or n.name == "__init__")
        }
        assert methods == source_methods, (
            f"missing from the stub: {sorted(source_methods - methods)}; "
            f"in the stub but not on GeneticsClient: {sorted(methods - source_methods)}"
        )
        assert set(exported) <= methods, (
            f"exported but not on GeneticsClient in the stub: {sorted(set(exported) - methods)}"
        )
        private = {n for n in methods if n.startswith("_") and n != "__init__"}
        assert not private, f"private methods leaked into the stub: {sorted(private)}"

    @check("every stub is valid Python")
    def _stub_syntax():
        for name, content in stub_files.items():
            try:
                ast.parse(content)
            except SyntaxError as exc:
                raise AssertionError(f"{name}: {exc}") from None

    @check("stub signatures track the SDK source rather than a copy")
    def _stub_derived():
        """Same property as the schema rules: rename an argument in the SDK and the
        stub must follow. Nothing about the surface may be spelled out here."""
        client_src = open(os.path.join(sdk_dir, "client.py")).read()
        assert "credible_sets" not in generator_code, (
            "the generator names an SDK function; the surface must come from _FUNCTIONS"
        )
        for token in ("qtl_gene", "leads_only", "GeneticsUsageError"):
            assert token in client_src or token in open(
                os.path.join(sdk_dir, "errors.py")
            ).read(), f"{token} vanished from the SDK — update this check"
            assert any(token in c for c in stub_files.values()), (
                f"{token} is in the SDK source but in no stub"
            )

    @check("the committed stubs match a fresh generation")
    def _stubs_fresh():
        for name, content in stub_files.items():
            path = os.path.join(STUBS_DIR, name)
            assert os.path.exists(path), f"{name} not committed"
            assert open(path).read() == content, (
                f"{name} differs from a fresh generation — run scripts/gen-sandbox-docs.py"
            )

    @check("the SDK source is never resolved to the staged sandbox/.sdk-src")
    def _never_the_staged_copy():
        """build.sh stages sandbox/.sdk-src and deletes it on
        an EXIT trap, so a copy found on disk means an interrupted build — the old default
        was reachable only when it was already stale, and regenerating from it rewrites
        stubs that ship inside the image. Simulate a leftover and assert it is not chosen."""
        staged = gen.STAGED_SDK_SRC
        planted = os.path.join(staged, "src", "genetics_mcp_server", "sdk")
        pre_existing = os.path.isdir(staged)
        if not pre_existing:
            os.makedirs(planted, exist_ok=True)
        try:
            env = {k: os.environ.pop(k) for k in ("GENETICS_SDK_SRC", "MCP_SERVER_DIR")
                   if k in os.environ}
            try:
                resolved = gen.resolve_sdk_src(None)
            finally:
                os.environ.update(env)
            assert resolved is None or os.path.abspath(resolved) != os.path.abspath(staged), (
                f"resolve_sdk_src fell back to the staged copy {staged}"
            )
        finally:
            if not pre_existing:
                shutil.rmtree(staged, ignore_errors=True)

    print(f"sandbox docs checks: {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
