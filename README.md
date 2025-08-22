# scaffold-repo

A small, batteries‑included tool to **stamp opinionated templates into one or many C/C++ repositories** and **enforce OSS hygiene** (SPDX headers, NOTICE, license texts). You point it at a repo (or a monorepo root) and, using a simple YAML model, it derives CMake, scaffolds tests/apps/docs/site, and keeps your license files correct and up‑to‑date.

---

## Highlights

* **Single‑ or multi‑project**: scaffold one project, a comma‑separated list, or **`--project all`** in dependency order.
* **Clone/build/install external git libs** in the same CLI (`--libs-*` flags) with safe step sanitization.
* **Export‑ready CMake** for libraries (regular or header‑only) with **variants** (`debug`, `memory`, `static`, `shared`) and coverage support.
* **Apps & tests** scaffolding with unioned `find_package`/`link_libraries` computed once per suite.
* **OSS hygiene**: inserts/updates SPDX headers, rebuilds NOTICE, ensures license texts match canonical copies.
* **Idempotent**: shows a plan; updates are prompted unless `--assume-yes`. `.gitignore` is applied early so validation respects your ignores.

---

## What it does

* **Applies templates** (Jinja2) into your repo(s):

  * `CMakeLists.txt` for libraries (regular or header‑only) with exported `*Config.cmake`.
  * `build.sh` (generator‑aware) with `build`, `install`, `coverage`, and `clean`.
  * Optional **tests/** scaffold and **apps/** trees (per “app context”).
  * Optional docs/site/git/changelog bits (`BUILDING.md`, `_config.yml`, Jekyll layout, `.gitignore`, Changie config).
  * Optional Dockerfile that can build third‑party deps from source.

* **Derives CMake deps** from a simple `libraries:` section:

  * Populates `find_package(...)`, `target_link_libraries(...)`, exported config dependencies, and apt build packages for **system** deps.

* **Enforces OSS hygiene**:

  * Adds/updates **SPDX license headers** at the top of supported file types (C/C++, CMake, Python, JS, CSS, HTML, etc.).
  * (Re)builds a **NOTICE** file from actual profiles used in your tree (stable order).
  * Ensures license texts match canonical resources; supports per‑path overrides and extras.

* **Clones/builds external libraries** (when requested):

  * Resolves dependency order across `kind: git` libs.
  * Sanitizes library‐provided `build_steps` (skips `git clone`, `rm -rf`, and installs during a build‑only phase).
  * Falls back to a generic CMake flow when no `build_steps` are provided.

---

## Quick start

### 1) Single project (apply in‑place)

```bash
# from your project root (or pass a path)
scaffold-repo .
# or, if running as a module:
python -m scaffold_repo.scaffold_repo .
```

*Review the summary; approve updates when prompted. Then build:*

```bash
./build.sh install
```

### 2) Multi‑project (monorepo‑style)

```bash
# write each requested project into <ROOT>/repos/<slug>/
scaffold-repo /path/to/mono --project "the-io-library,a-json-library" --assume-yes
# all “git_build” libraries, in dependency order:
scaffold-repo /path/to/mono --project all --assume-yes
```

### 3) External git libraries (clone/build/install)

```bash
# after scaffolding (or independently), build third‑party libs described in the config
scaffold-repo /path/to/mono --libs-install
# only some libs:
scaffold-repo /path/to/mono --libs-install --libs-only "restinio,fmt,asio"
# build (no install):
scaffold-repo /path/to/mono --libs-build
# clone only:
scaffold-repo /path/to/mono --libs-clone
```

> **Bring your own templates/config:** pass `--templates_dir /path/to/templates`.
> The loader reads `templates_dir/scaffold-repo.yaml` and all templates under that directory; otherwise it uses the package’s built‑in defaults.

---

## CLI

```
scaffold-repo [REPO=.]
  --project NAME[,NAME...]    Scaffold a specific set of projects
                              (use 'all' for every git_build project in dep order)
  --templates_dir PATH        Override templates root (must contain scaffold-repo.yaml)

  --assume-yes, -y            Apply file updates without prompting
  --show-diffs                Show diffs for updates before applying
  --no-prompt                 Do not prompt during SPDX header fixups
  --notice-file NAME          NOTICE filename (default: NOTICE)
  --decisions PATH            Path to JSON cache for SPDX replacement decisions
  --format text|json          Output format for results

  # External libraries (clone/build/install)
  --libs-clone                Clone only
  --libs-build                Clone + build (no install)
  --libs-install              Clone + build + install (default if any --libs-* given)
  --libs-only NAMES           Comma-separated subset of library names/slugs
```

**Exit codes:** `0` success; `1` if validation issues remain; `2/3` on config/template errors.

---

## Templates & configuration

* Templates are Jinja2. Each template may include tiny front‑matter:

  ```jinja
  {#- scaffold-repo:
      context: cmake
      dest: CMakeLists.txt
      updatable: true
      header_managed: true
    -#}
  ```

  * `context` selects a render context (e.g., `.`, `cmake`, `tests`, `apps.<name>`).
  * `dest` overrides the output path.
  * `updatable` controls whether a rendered file is auto‑eligible for updates next runs.
  * `header_managed` tells the diff engine to ignore SPDX headers when comparing.

* The primary model lives in `scaffold-repo.yaml` (in your `--templates_dir` or the packaged defaults). It defines:

  * Top‑level project knobs (language, standards, etc.).
  * A `libraries:` list for your project and its deps:

    * `kind: system` (with `find_package`/`link`/`pkg`)
    * `kind: git` (with `url`, optional `branch`, `dir`, `build_steps`)
    * Optional `template: git_build` to inherit common settings.
  * `tests:` targets and `apps:` contexts (both get dependency derivations).
  * Licensing profiles (`licenses:`), overrides and extras.

---

## CMake & build

### Library flavors

* **Header‑only** (`kind: header_only`): generates an `INTERFACE` target and exported config.
* **Compiled library**: generates four variants — `debug`, `memory`, `static`, `shared` — **all built and installable**.
  An umbrella target **`<name>::<name>`** points to the selected variant during in‑tree builds.

**Variant selection in consumers** is handled by the exported config via:

```cmake
set(A_BUILD_VARIANT "debug") # or memory|static|shared
find_package(<name> CONFIG REQUIRED)
target_link_libraries(myexe PRIVATE <name>::<name>)
```

### Coverage

* Enable coverage for developers with `-DA_ENABLE_COVERAGE=ON` (Clang and GCC supported).
* Tests include a `coverage_report` target that emits an HTML report.

### Build script (`build.sh`)

```bash
./build.sh build      # default
./build.sh install
./build.sh coverage   # builds tests, runs them, and emits HTML coverage
./build.sh clean
```

* Auto‑detects a generator (prefers Ninja; falls back to Unix Makefiles).
* Knobs via env vars: `PREFIX`, `BUILD_DIR`, `BUILD_TYPE`, `GENERATOR`.

---

## Tests

Define **suite targets** under `tests:`; the tool normalizes simple forms:

```yaml
tests:
  targets:
    - smoke                 # -> tests/src/smoke.c
    - custom:
        sources: [tests/src/custom.c]
        depends_on: [ZLIB]  # widens the suite’s dependency union
```

The suite’s `find_package` and `link_libraries` are derived once from the union of targets and any `tests.depends_on`.

---

## Apps

Add **app contexts** under `apps:` to build example/demo binaries. The tool computes suite‑level deps once and renders a small apps project:

```yaml
apps:
  context:
    dest: examples                  # base dir (default: apps)
    depends_on: [the-io-library]    # defaults to the project lib if omitted
  map-reduce:
    binaries:
      - dump_files_1
      - list_files
```

Each context gets its own `CMakeLists.txt` and a helper `build.sh`.

---

## External git libraries (optional phase)

Describe third‑party source deps in `libraries:` with `kind: git`. You can provide `build_steps` (shell lines), or rely on the **generic CMake fallback**. The **`--libs-*`** flags:

* Compute a dependency‑ordered plan.
* **Sanitize** steps (we manage clones; `rm -rf` is skipped; install steps are removed in `--libs-build`).
* Respect a `dir` hint for nested source layouts, and use `--branch`/`--depth 1` when configured.
* Provide `nproc` fallbacks on macOS/BSD.

---

## OSS hygiene (SPDX/NOTICE/licenses)

* Inserts or updates SPDX headers at the top of supported files (correct comment style per language).
* Keeps a **NOTICE** file built from the profiles actually exercised by your tree (default profile first, then first‑seen order).
* Ensures license text files match canonical content (`license_canonical`), and will create/update them as needed.
* Honors per‑path **profile overrides** and **extras** (e.g., third‑party headers or separate license files).
* Respects your `.gitignore` (applied early).
  Use `--decisions` to cache “replace this header with that one” choices across runs.

---

## Dockerfile (optional)

A generated Dockerfile:

* Installs build tools and a specific CMake version.
* Optionally installs `cmake.apt_dev_packages`.
* Emits your `kind: git` libraries’ `build_steps` to build/install them.
* Builds and installs your project.

---

## Idempotence & prompts

* You’ll see two summaries: **Jinja templates** (rendered) and **non‑Jinja files** (verbatim copies).
* Jinja updates are **batched** behind one prompt; non‑Jinja updates prompt individually.
* Pass `--assume-yes` to apply without prompting, and `--show-diffs` to view diffs inline.
* After applying, the tool immediately runs OSS fixes silently (so SPDX/NOTICE stay coherent).

---

## Examples

```bash
# Scaffold two projects into a monorepo, then build external libs
scaffold-repo ~/work/mono --project "the-io-library,a-json-library" -y --show-diffs
scaffold-repo ~/work/mono --libs-install

# Apply templates to an existing single repo (in-place)
scaffold-repo . -y --show-diffs

# Use your own template pack
scaffold-repo ~/work/mono --project all --templates_dir ~/my-templates -y
```

---

## License

This tool’s templates default to **Apache‑2.0** and include helpers for other licenses.
Your generated repositories can use whatever license you configure via `licenses:`.

---
