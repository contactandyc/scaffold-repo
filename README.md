# scaffold-repo

A small, batteries‑included tool to **stamp opinionated templates into a C/C++ repository** and **enforce OSS hygiene** (SPDX headers, NOTICE, license texts). You describe your project once in `scaffold-repo.yaml`; `scaffold-repo` derives CMake configuration, optional tests/apps scaffolding, a `BUILDING.md`, a Dockerfile, and keeps SPDX/NOTICE/license files correct and up‑to‑date.

---

## What it does

* **Applies templates** (Jinja2) into your repo:

  * `CMakeLists.txt` for libraries (regular or header‑only).
  * `BUILDING.md` and `build_install.sh` (multi‑variant, generator‑aware).
  * Optional **tests** scaffold and **example apps** tree.
  * Optional site bits (`_config.yml`, Jekyll layout), `.gitignore`, Changie config.
  * Optional Dockerfile that can build third‑party deps from source.
* **Derives CMake deps** from a simple `libraries:` section:

  * Populates `find_package(...)`, `target_link_libraries(...)`,
    generates exported `*Config.cmake` + `*ConfigVersion.cmake`.
  * Computes apt build packages for transitive **system** deps.
* **Test + apps helpers**:

  * Normalize `tests.targets` and compute suite‑level `find_package`/`link_libraries`.
  * Normalize `apps` contexts and derive per‑suite linking once.
* **Enforces OSS hygiene**:

  * Adds/updates **SPDX license headers** at the top of source files (C/C++, CMake, Python, JS, CSS, HTML, etc.).
  * (Re)builds a **NOTICE** file from license profiles.
  * Ensures license texts exist and match canonical content.
  * Supports **per‑path license profile overrides** and extra third‑party license files.
* **Idempotent & cautious**:

  * First run writes files. On subsequent runs it shows a summary and asks before updating existing files.
  * SPDX headers are managed automatically and kept out of the diff noise.

---

## Quick start

1. **Add a `scaffold-repo.yaml`** to the root of your C/C++ project. Minimal example:

   ```yaml
   project_name: mylib
   version: 0.1.0
   language: "C"           # or "C CXX"
   packages: [git, build, docs, tests, changie]   # pick what you want stamped

   cmake:
     kind: library
     sources:
       - src/foo.c
       - src/bar.c

   # Tell scaffold-repo what your lib depends on; it will derive CMake bits:
   libraries:
     - name: mylib
       depends_on: [ZLIB, OpenSSL]

     - name: ZLIB
       kind: system
       find_package: "ZLIB REQUIRED"
       link: "ZLIB::ZLIB"
       pkg: zlib1g-dev

     - name: OpenSSL
       kind: system
       find_package: "OpenSSL REQUIRED"
       link: "OpenSSL::Crypto"
       pkg: libssl-dev
   ```

2. **Run the tool** (from the project root):

   ```bash
   # If installed with an entrypoint:
   scaffold-repo .

   # Or via Python:
   python -m scaffold_repo.cli .
   ```

3. Review the summary, approve updates (if prompted), and **commit** the changes.

4. Build your project:

   ```bash
   ./build_install.sh              # one-shot build+install (multi-variant)
   # or:
   mkdir build && cd build
   cmake .. -DCMAKE_BUILD_TYPE=Release
   cmake --build . -j"$(nproc)"
   sudo cmake --install .
   ```

---

## CLI

```
scaffold-repo [REPO=.]
  --fix-licenses           Apply fixes for SPDX/NOTICE/license texts during validation
  --no-prompt              Do not prompt while fixing SPDX headers (license step)
  --notice-file NAME       NOTICE file name (default: NOTICE)
  --decisions PATH         Path to JSON cache for SPDX replacement decisions
  --format text|json       Output validation summary (default: text)
```

* **Exit code:** `0` success, `1` if validation issues remain, `2/3` on config/template errors.

---

## What gets generated (by package)

Enable a package by listing it under `packages:` in `scaffold-repo.yaml`.

| Package    | Files (relative to repo root)                   |
| ---------- | ----------------------------------------------- |
| `git`      | `.gitignore`                                    |
| `build`    | `build_install.sh`                              |
| `docs`     | `BUILDING.md`                                   |
| `tests`    | `tests/**` (CMake + your targets)               |
| `changie`  | `.changie.yaml`, `.changes/**`                  |
| `site`     | `_config.yml`, `_layouts/default.html`          |
| *(always)* | `CMakeLists.txt`, `LICENSE`, `README.md` (stub) |

> Tip: Skip `site` if you already manage `_config.yml`/`_layouts`; skip `docs` if you maintain your own `BUILDING.md`.

---

## `scaffold-repo.yaml` reference (most common keys)

Top‑level:

```yaml
project_name: mylib
project_title: My Library          # optional, cosmetic
version: 0.1.0
language: "C" or "C CXX"
c_standard: 99                     # default 99
cxx_standard: 17                   # default 17 (when CXX enabled)
pic: true                          # default true
license_spdx: Apache-2.0           # token used in notices
author: "Your Name"
email: you@example.com
date: 2025-08-08                   # used to compute year for notices
packages: [git, build, docs, tests, changie, site]
templates_dir: ./my-templates      # optional: your own template root
```

### `cmake:` (project build)

```yaml
cmake:
  kind: library            # or "header_only"
  sources: [src/foo.c, ...]
  default_variant: debug   # affects export/import name selection
  find_packages: ["ZLIB REQUIRED", ...]    # filled automatically when possible
  link_libraries: ["ZLIB::ZLIB", ...]      # filled automatically when possible
  deps_for_config: [ZLIB]                  # used in generated *Config.cmake
  apt_packages: [zlib1g-dev, ...]          # derived from libraries (system deps)
  apt_dev_packages: [valgrind, ...]        # dev tools; get added to Dockerfile
```

* **Header‑only**: set `kind: header_only`; optional `namespace`, `header_only_c_std`, and `header_only_config_in` are supported.

### `libraries:` (single source of truth for deps)

Describe project + third‑party deps. You can mix:

* **system** libraries:

  ```yaml
  - name: ZLIB
    kind: system
    find_package: "ZLIB REQUIRED"
    link: "ZLIB::ZLIB"
    pkg: zlib1g-dev
  ```

* **git** / **tar** sources (built in Dockerfile and shown in BUILDING.md):

  ```yaml
  - name: asio
    kind: git
    url: https://github.com/chriskohlhoff/asio.git
    branch: asio-1-30-2
    build_steps:
      - git clone --depth 1 --branch asio-1-30-2 --single-branch "https://..." "asio"
      - cd asio/asio
      - ./autogen.sh
      - cd ../../
      - mkdir -p build/asio
      - cd build/asio
      - ../../asio/asio/configure --prefix=/usr/local
      - make -j"$(nproc)"
      - sudo make install
    find_package: "asio REQUIRED"
    link: "asio::asio"
  ```

* **Project library**: add an entry **named like your project** and declare its direct deps:

  ```yaml
  - name: mylib
    depends_on: [ZLIB, OpenSSL]   # names resolved slug/underscore-insensitively
  ```

`scaffold-repo` resolves transitive deps, orders them, and populates `cmake.find_packages`, `cmake.link_libraries`, `cmake.deps_for_config`, and a list of apt packages for **system** deps.

### `tests:` (optional)

```yaml
tests:
  depends_on: [mylib]   # optional; defaults to the project lib
  targets:
    - smoke             # => tests/src/smoke.c
    - hello_world
    - custom:
        sources: [tests/src/custom.c]
        depends_on: [ZLIB]    # widens the union for suite deps
```

* Targets become `add_executable(...)` and `add_test(...)`.
* Suite‑level `find_package`/`link_libraries` are derived once from the union of deps.

### `apps:` (optional example binaries)

```yaml
apps:
  context:
    dest: examples                 # base dir (default: apps)
    depends_on: [mylib]
  echo_server:
    dest: 01_echo                  # per-context override; under base
    binaries:
      - echo
      - special:
          sources: [examples/01_echo/src/special.c]
          link_libraries: ["mylib::mylib"]    # optional per-binary override
```

Each app context gets:

* A dedicated `CMakeLists.txt` with suite‑level `find_package`/`link_libraries` derived once.
* Default sources under `<dest>/src/<name>.c` if you provide a bare name.

### Licensing & NOTICE

```yaml
license_profile: company-default
license_overrides:
  "third_party/**": third-party

licenses:
  company-default:
    spdx: |
      SPDX-FileCopyrightText: 2024–{{ year }} Your Co
      SPDX-License-Identifier: Apache-2.0
    license: LICENSE
    license_canonical: resources/licenses/Apache-2.0.txt
    notice_template: resources/notices/standard_notice.j2
    # or inline:
    notice: |
      {{ project_name }} © {{ year }} Your Co
      Licensed under Apache-2.0 (see LICENSE)

  third-party:
    spdx: |
      SPDX-FileCopyrightText: 2017–{{ year }} Upstream
      SPDX-License-Identifier: BSD-3-Clause
    extra_licenses:
      - path: LICENSE.upstream
        canonical: resources/licenses/BSD-3-Clause.txt
```

* **SPDX headers** are inserted at the top of supported file types using the correct comment style.
* **NOTICE** is rebuilt from profiles actually used in the repo.
* **License texts** in `license` and any `extra_licenses` are kept in sync with canonical resources.

---

## Templating & customization

* Templates are Jinja2. You can point to **your own** tree via `templates_dir:` in `scaffold-repo.yaml`.

* Templates may carry a tiny front‑matter (YAML inside a Jinja comment) to set context/destination, but you rarely need to touch that. Example:

  ```jinja
  {#- scaffold-repo:
      context: cmake
      dest: CMakeLists.txt
      updatable: true
      header_managed: true
    -#}
  ```

* Contexts used by the built‑ins:

  * `.` (base), `cmake`, `tests`, and `apps.<name>` (one per app context).

---

## The build script (`build_install.sh`)

* Detects a generator (prefers Ninja), creates **variant** builds (`debug`, `memory`, `coverage`, `static`, `shared`), and installs all.
* Useful knobs (env vars): `GENERATOR`, `VARIANTS`, `PREFIX`, `SKIP`, `CLEAN`, `AUTO_CLEAN`, `EXTRA_CMAKE_ARGS`.
* CMake exposes `A_BUILD_VARIANT` so your exported package can select the right imported target.

---

## Dockerfile (optional)

* Installs base build tools + a chosen CMake version.
* Optionally installs your `cmake.apt_dev_packages`.
* For `libraries` with `kind: git`/`tar` and `build_steps`, the Dockerfile emits those steps to build/install deps before building your project.
* Build args: `UBUNTU_TAG`, `CMAKE_VERSION`, `CMAKE_BASE_URL`, `GITHUB_TOKEN`.

---

## Idempotence, prompts & state

* On each run you get two summaries:

  * **Jinja templates** (rendered files).
  * **Non‑Jinja files** (verbatim copies).
* First run applies everything. On later runs, updates are **interactive**.
* After applying, the tool auto‑runs OSS validation with silent fixes to add missing SPDX headers.
* State is recorded in **`.scaffold-repo.yaml`** (content/template hashes). It’s safe to commit.

---

## Typical workflows

* **Bootstrap a new lib**: write minimal `scaffold-repo.yaml`, run `scaffold-repo`, push.
* **Add a new dep**: update `libraries:`, re‑run; CMake + BUILDING/Dockerfile update.
* **Add tests**: add `tests:` with targets; enable `tests` package; re‑run.
* **Add example apps**: add `apps:` contexts; re‑run.
* **Enforce licenses in CI**: run with `--format json`; fail the job if exit code is `1`.

---

## FAQ (short)

* **Will it overwrite my files?**
  It asks before updating existing files (after the first run). SPDX headers are managed automatically.

* **How do I skip pieces?**
  Remove packages from `packages:` (e.g., drop `site` or `docs`).

* **Do I need to vendor the `resources/` tree?**
  No. Canonical license texts are embedded in the tool. You can still opt to stamp them by enabling a custom package mapping.

* **How do I use my own templates?**
  Set `templates_dir:` to your folder. Same file names/contexts will be picked up.

---

## Example: tiny project

```yaml
project_name: hello-c
version: 0.1.0
packages: [git, build, docs]

cmake:
  kind: library
  sources: [src/hello.c]

libraries:
  - name: hello-c       # the project
    depends_on: [ZLIB]
  - name: ZLIB
    kind: system
    find_package: "ZLIB REQUIRED"
    link: "ZLIB::ZLIB"
    pkg: zlib1g-dev
```

Run `scaffold-repo .`, then `./build_install.sh`.

---

## License

This tool’s templates default to **Apache‑2.0** and include helpers for other licenses.
Your generated repositories can use whatever license you configure via `licenses:`.

---

**That’s it.** Point `scaffold-repo.yaml` at what you need; `scaffold-repo` will keep your C/C++ repo coherent, buildable, and compliant.
