# `scaffold-repo`

`scaffold-repo` is a registry-driven scaffolding engine and meta-package manager designed for C/C++ and polyglot repositories.

Rather than generating a repository once and leaving it unmanaged, `scaffold-repo` allows you to maintain multiple repositories from a centralized YAML registry. By defining library metadata, authorship, and dependency mappings in one place, the engine can perform a topological sort of your dependency graph, compute CMake linking requirements, apply open-source compliance headers, and synchronize these configurations across your designated repositories. This centralized approach allows you to apply broad structural changes—such as updating a minimum CMake version or standardizing a CI pipeline—across an entire project ecosystem via a single registry update and CLI execution.

---

## Core Capabilities

* **Graph-Aware Generation:** Parses `depends_on:` declarations to topologically sort dependencies. It automatically generates `CMakeLists.txt`, `build.sh`, and `Dockerfile` assets with the corresponding `find_package` and `target_link_libraries` configurations.
* **CMake Build Variants:** Generates four standard build configurations for C/C++ libraries (`debug`, `memory`, `static`, `shared`) alongside an umbrella target for downstream consumption.
* **OSS Compliance Management:** Injects and updates SPDX license headers across supported file types (including C, C++, Python, JS, HTML), aggregates `NOTICE` files based on repository lineage, and synchronizes canonical `LICENSE` texts.
* **Polyglot & Multi-App Routing:** Scaffolds base C/C++ libraries while routing distinct templates to sub-applications (e.g., combining C libraries with C++ binaries or Jekyll sites) using the `app-resources/` directory.
* **External Dependency Handling:** Clones, configures, and installs external Git dependencies (such as `libuv` or `fmt`) in the order required by your dependency graph.
* **State Management:** Compares normalized ASTs to minimize unnecessary diffs, respects `.gitignore` rules, and provides configuration options to freeze specific files from future template overwrites.

---

## Registry Architecture

The central registry is located within the `templates/` directory. It strictly separates YAML data from Jinja2/verbatim templates:

```text
templates/
├── .scaffold-defaults.yaml     # Internal engine router (specifies active packages)
├── libraries/                  # The registry of configured libraries
│   └── my-namespace/
│       └── my-library.yaml
├── profiles/                   # Global variables (authors, organizations, base licenses)
├── library-templates/          # Architectural archetypes (e.g., cmake-c-git)
├── licenses/                   # SPDX and NOTICE configurations
├── flavors/                    # Root-level Jinja templates (CMakeLists.txt, build.sh)
└── app-resources/              # Sub-application templates (isolated from the root)

```

### Declarative Configuration

A library and its sub-components are defined using a structured YAML format.

**`templates/libraries/my-namespace/a-bitset-library.yaml`**

```yaml
project_title: A Bitset Library
version: 0.0.1

# 1. Inherit global authors, tools, and the default OSS license
profile: my-namespace/my-profile

# 2. Assign the C/CMake architecture template
template: cmake-c-git

# 3. Define internal dependencies for CMake linking
depends_on:
  - my-namespace/a-memory-library

# 4. Define tests (auto-detected from src/ if omitted)
tests:
  targets:
    - test_bitset
    - test_bitset_expandable

# 5. Define sub-applications or examples
apps:
  context:
    dest: examples
  demo:
    binaries:
      - parse_sentences

```

---

## Generated Output

When executed against a C/C++ project, `scaffold-repo` produces a standardized repository structure.

### CMake and Library Variants

For compiled libraries, the engine outputs `debug`, `memory`, `static`, and `shared` variants, which can be built and installed concurrently. It also establishes an umbrella target, **`<name>::<name>`**, for consumer linking:

```cmake
set(A_BUILD_VARIANT "debug") # options: memory | static | shared
find_package(a_bitset_library CONFIG REQUIRED)
target_link_libraries(myexe PRIVATE a_bitset_library::a_bitset_library)

```

### Standardized Build Script

Each repository is populated with a generator-aware `build.sh` script (defaulting to Ninja, falling back to Make) that supports the following standard commands:

```bash
./build.sh build      # Configures and builds the project
./build.sh install    # Builds and installs the project (requests sudo if required)
./build.sh coverage   # Builds tests, executes them, and generates an HTML coverage report
./build.sh clean      # Removes build directories

```

---

## CLI Workflow and Usage

`scaffold-repo` auto-detects the registry if run from a directory containing a `templates/` folder. Alternatively, you can specify the path using `--templates-dir` or `-C`.

### The Branching Pipeline

The engine utilizes a 4-phase pipeline (Scaffold, Dependencies, Build, Git Sync) and enforces a **`main` -> `dev-*` -> `feat/***` branching topology. Generated code is isolated into a `../repos` directory to keep the primary workspace clear.

**Step 1: Start a Feature**

```bash
scaffold-repo my-namespace --start-feature "dynamic-resize" -y

```

> Clones missing repositories and synchronizes existing ones. It creates or checks out the integration branch (e.g., `dev-v0.0.2`), and branches `feat/dynamic-resize` for template scaffolding.

**Step 2: Compile Dependencies & First-Party Code**

```bash
scaffold-repo my-namespace --install-deps --install

```

> Compiles and installs external dependencies, then performs a topological execution of `./build.sh install` on the local C++ libraries.

**Step 3: Commit and Push**

```bash
scaffold-repo my-namespace --commit "WIP: integrating allocator" --push

```

> Commits changes to the feature branch and pushes to origin. The engine prevents direct commits to `main` or the `dev-*` integration branches during this step.

**Step 4: Publish Release**
*(Executed after merging PRs into the `dev-*` integration branch)*

```bash
scaffold-repo my-namespace --publish-release --push

```

> Checks out `main`, merges the specified `dev` branch, auto-tags the release based on the YAML version, bumps the version in the central registry, and pushes the updates.

### Complete CLI Reference

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

Git Orchestration:
  --diff                    Print the unpaginated Git diff for all targeted repos
  --commit MSG              Commit changes in target projects (blocked on 'main' and 'dev-*')
  --publish-feature         Merge current feature branch into dev branch and delete feature branch
  --drop-feature            Discard current feature branch (and uncommitted changes), return to dev
  --publish-release         Merge an integration branch into main, tag it, and bump YAML
  --push                    Push commits to origin (Pushes main+tags if used with --publish-release)
  --tag                     Tag targets using their YAML 'version' (e.g., 'v0.0.1')

```

---

## Advanced Template Configuration

### Jinja Front-Matter Routing

Template routing and file overwrite rules can be configured using a Jinja comment block at the top of `.j2` files:

```jinja
{#- scaffold-repo: { dest: "src/main.c", updatable: false, context: "cmake" } -#}

```

* `dest`: Overrides the default output directory.
* `updatable: false`: Instructs the engine to generate the file on the initial run but ignore it on subsequent runs, preserving manual edits.
* `context`: Shifts the Jinja dictionary evaluation root to a specific sub-key (e.g., scoping the template to the `tests:` block).

### Template Language Isolation

If the generated files utilize their own templating syntax (such as Go templates or Liquid), wrap the syntax in `{% raw %}` blocks to prevent the Jinja scanner from evaluating it:

```jinja
{% raw %}
versionFormat: '## {{.Version}} - {{.Time.Format "2006-01-02"}}'
{% endraw %}

```

### Path Stripping and Sub-Application Shielding

* **Automatic Stripping:** The engine automatically removes structural folder prefixes (like `flavors/cmake-c/`) when writing files to the target repository.
* **App Shielding:** Templates located in `templates/app-resources/<flavor>/` are excluded from the root repository generation cycle. They are routed exclusively to the sub-applications defined in the `apps:` block of the library's YAML configuration.
