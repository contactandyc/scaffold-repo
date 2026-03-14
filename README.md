# `scaffold-repo`: The Declarative Fleet Manager

`scaffold-repo` is a highly scalable, registry-driven scaffolding engine and meta-package manager for C/C++ (and polyglot) repositories.

Unlike traditional tools that ask you 20 questions to generate a single repository and then forget about it, `scaffold-repo` manages your **entire fleet** of repositories from a centralized YAML registry. You define what a library *is*, who its *authors* are, and what it *depends on*. The engine topological-sorts your dependency graph, computes the exact `CMake` linkings, enforces Open Source compliance, and stamps out the code across dozens of repositories instantly.

If you need to update the minimum CMake version, change a license, or add a new CI tool across 50 repositories, you change one line in the registry, run `scaffold-repo . --project all -y`, and your entire ecosystem is modernized in seconds.

---

## ✨ Highlights & Capabilities

* **Graph-Aware Meta-Generation:** Reads `depends_on:` links, topological-sorts your dependencies, and writes the perfect `CMakeLists.txt`, `build.sh`, and `Dockerfile` with the correct `find_package` and `target_link_libraries` configurations.
* **Export-Ready CMake Variants:** Automatically generates four build variants for every C/C++ library (`debug`, `memory`, `static`, `shared`) alongside an umbrella target, making them instantly consumable.
* **Built-in OSS Compliance:** Automatically injects and updates SPDX license headers (in C, C++, Python, JS, HTML, etc.), aggregates `NOTICE` files based on actual code lineage, and syncs canonical `LICENSE` texts.
* **Polyglot & Multi-App Support:** Scaffolds massive C/C++ libraries while simultaneously routing specific templates for sub-applications (e.g., mixing C libraries with C++ binaries or Jekyll sites) using the `app-resources/` engine.
* **External Dependency Builder:** Acts as a lightweight package manager to safely clone, configure, and install external Git libraries (like `libuv` or `fmt`) in the exact order your project needs them.
* **Idempotent & Safe:** Compares normalized ASTs to prevent ping-ponging diffs, respects `.gitignore`, and allows you to freeze custom files from future updates.

---

## 📂 The Registry Architecture

Your centralized registry lives in the `templates/` directory. It strictly separates **data** (YAML) from **templates** (Jinja2/verbatim files):

```text
templates/
├── .scaffold-defaults.yaml     # Internal engine router (which packages to enable)
├── libraries/                  # The registry of every library in your fleet
│   └── my-namespace/
│       └── my-library.yaml
├── profiles/                   # Global variables (authors, orgs, base licenses)
├── library-templates/          # Architectural Archetypes (e.g., cmake-c-git)
├── licenses/                   # SPDX and NOTICE configurations
├── flavors/                    # Root-level Jinja templates (CMakeLists.txt, build.sh)
└── app-resources/              # Sub-application templates (shielded from the root)

```

### A Clean, Declarative DSL

Because of the deep-merging engine, defining a new library, its tests, and its apps takes only a few lines of code.

**`templates/libraries/my-namespace/a-bitset-library.yaml`**

```yaml
project_title: A Bitset Library
version: 0.0.1

# 1. Inherit global authors, tools, and the default OSS license
profile: my-namespace/my-profile

# 2. Inherit the C/CMake architecture (CMakeLists, Dockerfile, tests)
template: cmake-c-git

# 3. The engine will automatically link these via CMake!
depends_on:
  - my-namespace/a-memory-library

# 4. Define tests (auto-detects from src/ if omitted)
tests:
  targets:
    - test_bitset
    - test_bitset_expandable

# 5. Define sub-applications/examples
apps:
  context:
    dest: examples
  demo:
    binaries:
      - parse_sentences

```

---

## 🏗️ What Developers Get (The Output)

When `scaffold-repo` runs against a C/C++ project, it generates an enterprise-grade repository environment.

### Library Flavors & CMake

For compiled libraries, the engine generates four variants — `debug`, `memory`, `static`, `shared` — **all built and installable simultaneously**.
An umbrella target **`<name>::<name>`** is created. Consumers can easily select which variant they want to link against:

```cmake
set(A_BUILD_VARIANT "debug") # or memory|static|shared
find_package(a_bitset_library CONFIG REQUIRED)
target_link_libraries(myexe PRIVATE a_bitset_library::a_bitset_library)

```

### The Standard Build Script (`build.sh`)

Every repo gets a generator-aware build script (prefers Ninja, falls back to Make) with standardized commands:

```bash
./build.sh build      # default: configure and build
./build.sh install    # build + install (with sudo if needed)
./build.sh coverage   # builds tests, runs them, and emits an HTML coverage report
./build.sh clean      # wipes build directories

```

## 🚀 CLI Usage

*(Note: If you run the CLI from a directory containing a `templates/` folder, the engine will auto-detect it. Otherwise, you can pass `--templates-dir` or use `-C` to change the working directory).*

### 1. The Full Pipeline (Build, Test, and Ship)

Because `scaffold-repo` topological-sorts your registry, you can orchestrate a full end-to-end pipeline across dozens of repositories with a single command. 

```bash
scaffold-repo my-namespace --install-deps --install -y --commit "Update CMake config" --tag --push

```

When you run this, the engine executes four distinct phases:

1. **Phase 1 (Scaffold):** Auto-clones missing repositories and applies templates to all targets inside `my-namespace`.
2. **Phase 2 (Dependencies):** Compiles and installs any external third-party C/C++ packages they need (e.g., `libuv`).
3. **Phase 3 (First-Party Build):** Steps through your scaffolded code in dependency-order and executes `./build.sh install` on each one.
4. **Phase 4 (Git Sync):** Commits the files, generates an annotated Git tag based on the `version:` field inside your YAML registry, and pushes everything to GitHub!

### 2. Granular Commands

If you just want to update files or build a specific component:

```bash
# Scaffold one specific project (auto-cloning if missing)
scaffold-repo my-namespace/a-bitset-library -y --show-diffs

# Only build a specific third-party dependency 
scaffold-repo common/libuv custom/fmt --build-deps

# Just cut a release without making code changes (creates tag from YAML version and pushes)
scaffold-repo my-namespace/a-bitset-library --tag --push

```

### Full CLI Reference

```text
Usage: scaffold-repo [PROJECTS...] [OPTIONS]

Arguments:
  PROJECTS                  One or more projects/namespaces to scaffold or build.
                            (e.g., 'my-namespace/a-bitset-library', 'common', 'all')

Workspace Options:
  -C, --cwd PATH            Run as if started in <PATH> (default: current dir)
  --templates-dir PATH      Override templates directory (auto-detects ./templates)

Scaffolding Options:
  -y, --assume-yes          Apply template updates and fix licenses without prompting
  --show-diffs              Print inline diffs before applying file updates
  --no-prompt               Do not prompt during SPDX license header fixups

Dependency Lifecycle:
  --clone-deps              Fetch external dependencies without compiling
  --build-deps              Fetch and compile external dependencies
  --install-deps            Fetch, compile, and install external dependencies

Target Lifecycle (Your Code):
  --build                   Run './build.sh build' on the scaffolded projects
  --install                 Run './build.sh install' on the scaffolded projects

Git Orchestration (Your Code):
  --commit MSG              Commit all changes in target projects with this message
  --tag                     Tag targets using their YAML 'version' (e.g., 'v0.0.1')
  --push                    Push commits (and tags) to the remote origin

```

## 🚀 CLI Usage

*(Note: If you run the CLI from a directory containing a `templates/` folder, the engine will auto-detect it. Otherwise, you can pass `--templates-dir` or use `-C` to change the working directory).*

### The 4-Phase Monorepo Pipeline

`scaffold-repo` operates on a 4-phase pipeline (Scaffold, Dependencies, Build, Git Sync). It enforces a strict **`main` -> `dev-*` -> `feat/*`** branching topology, and isolates all generated code into a `../repos` directory so your tooling workspace stays perfectly clean.

**Step 1: Start a New Feature**
```bash
scaffold-repo my-namespace --start-feature "dynamic-resize" -y

```

*(Auto-clones missing repos, auto-syncs existing ones. Creates the integration branch `dev-v0.0.2` if it doesn't exist, checks out `feat/dynamic-resize` off of it, and scaffolds the templates).*

**Step 2: Compile Dependencies & First-Party Code**

```bash
scaffold-repo my-namespace --install-deps --install

```

*(Compiles/installs external dependencies, then topologically runs `./build.sh install` on your C++ libraries).*

**Step 3: Commit and Push to the Feature Branch**

```bash
scaffold-repo my-namespace --commit "WIP: integrating allocator" --push

```

*(Safely commits to your feature branch and pushes to origin, aggressively blocking accidental commits to `main` or the `dev-*` integration branch).*

**Step 4: The Release**
*(After you merge your PRs into the `dev-*` integration branch on GitHub)*:

```bash
scaffold-repo my-namespace --publish-release --push

```

*(Checks out `main`, prompts you for the `dev` branch to release, merges it into `main`, auto-tags it, bumps the version in your central YAML registry, and pushes the new release).*

---

### Full CLI Reference

```text
Usage: scaffold-repo [PROJECTS...] [OPTIONS]

Arguments:
  PROJECTS                  One or more projects/namespaces to scaffold or build.
                            (e.g., 'my-namespace/a-bitset-library', 'common', 'all')

Workspace Options:
  -C, --cwd PATH            Run as if started in <PATH> (default: current dir)
  --templates-dir PATH      Override templates directory (auto-detects ./templates)
  --start-feature NAME      Start a feature branch off the integration dev branch
                            (Auto-computes the target dev branch from the YAML version)

Scaffolding Options:
  -y, --assume-yes          Apply template updates and fix licenses without prompting
  --show-diffs              Print inline diffs before applying file updates
  --no-prompt               Do not prompt during SPDX license header fixups

Dependency Lifecycle:
  --clone-deps              Fetch external dependencies without compiling
  --build-deps              Fetch and compile external dependencies
  --install-deps            Fetch, compile, and install external dependencies

Target Lifecycle (Your Code):
  --build                   Run './build.sh build' on the scaffolded projects
  --install                 Run './build.sh install' on the scaffolded projects

Git Orchestration (Your Code):
  --diff                    Print the unpaginated Git diff for all targeted repos (skips other actions)
  --commit MSG              Commit changes in target projects (blocked on 'main' and 'dev-*')
  --publish-feature         Merge current feature branch into dev branch and delete feature branch
  --drop-feature            Discard current feature branch (and uncommitted changes), return to dev
  --publish-release         Merge an integration branch into main, tag it, and bump YAML
  --push                    Push commits to origin (If used with --publish-release, pushes main+tags)

```

---

## 🧠 Advanced Template Features

### Jinja Front-Matter Routing

You can control exactly where files go, and whether they can be overwritten, by placing a tiny Jinja comment at the top of your `.j2` templates:

```jinja
{#- scaffold-repo: { dest: "src/main.c", updatable: false, context: "cmake" } -#}

```

* `dest`: Overrides the default output path. (By default, structural folders like `flavors/cmake-c/` are automatically stripped).
* `updatable: false`: The engine will generate this file the first time, but will **never** attempt to overwrite it on future runs, keeping manual code safe.
* `context`: Shifts the Jinja dictionary root to a specific sub-key (e.g., evaluating inside the `tests:` block).

### Raw Blocks (Protecting other template languages)

If you are generating files that use their own templating syntax (like Go templates for `Changie` or Liquid templates for `Jekyll`), wrap the payload in `{% raw %}` so the Jinja scanner ignores it:

```jinja
{% raw %}
versionFormat: '## {{.Version}} - {{.Time.Format "2006-01-02"}}'
{% endraw %}

```

### Automatic Path Stripping

You can organize your templates into logical folders (like `flavors/cmake-c/tests/CMakeLists.txt.j2`). The engine will automatically strip the `flavors/cmake-c/` prefix and output the file cleanly to `tests/CMakeLists.txt` in the target repository.

### Polyglot App Shielding

Templates placed in `templates/app-resources/<flavor>/` are entirely shielded from the root generation cycle. They are exclusively routed to sub-applications defined in your YAML's `apps:` block, allowing you to mix C libraries with C++ binaries seamlessly.
