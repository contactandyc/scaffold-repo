# Authoring Templates for `scaffold-repo`

The `scaffold-repo` CLI is a purely declarative execution engine. It contains no hardcoded opinions about your build system, your CI/CD pipeline, or your folder structures. All of that power is defined entirely within your **Template Registry**.

This guide explains how to write your own templates, configure language stacks, harness the engine's dynamic routing logic, and utilize the Overlay pattern to extend the system without forking it.

---

## 1. The Registry Directory Structure (Separation of Concerns)

A Template Registry is a directory containing YAML configurations, Jinja2 templates, and licensing rules. The engine expects a specific directory layout to properly separate organizational data from technical implementation:

```text
templates/
├── .scaffold-defaults.yaml    # Global router: defines global variables, prompts, and packages
├── stacks/                    # The technical implementations (e.g., c/cmake, python/standard)
├── profiles/                  # Organizational standards (author info, default licenses, tools)
├── mixins/                    # Modular, optional Jinja templates (changie, jekyll-site)
├── library-templates/         # Archetype YAML configs for dependencies (e.g., cmake-c-git)
├── libraries/                 # The global dependency index (how to build/link external tools)
├── licenses/                  # SPDX logic and NOTICE file generators
├── app-resources/             # Special templates looped per sub-application inside a repo
└── resources/                 # Global assets like aliases.yaml or canonical license texts
```

### The Intuition: Why separate Stacks and Profiles?
If you put Author information or License requirements directly into your Python templates, you have to duplicate them for your C++ templates. By separating them, the **Stack** (`stacks/python/standard`) only cares about *how to build Python*, while the **Profile** (`profiles/my-org/backend-team.yaml`) dictates *who owns it and how it is licensed*. The engine magically merges them together.

---

## 2. The Overlay Pattern

`scaffold-repo` supports an **Overlay Architecture**. This allows a company to maintain a centralized "Base Registry" (pulled via Git), while individual teams maintain a local "Overlay Directory" for their specific needs.

If the engine looks for a template (e.g., `stacks/python/standard/build.sh.j2`), it searches the virtual filesystem in this order:
1. **The Local Overlay** (e.g., `./local-templates/`)
2. **The Base Registry** (e.g., `~/.cache/scaffold-repo/company-base/`)

**The Intuition:** A specific team might want to add a custom linting step to the company's standard Python `build.sh`. Instead of forking the entire company registry, they simply drop their modified `build.sh.j2` into their local overlay. The engine natively overrides the base file while still pulling `pyproject.toml` and `AUTHORS` from the company base.

---

## 3. The Data Merge Pipeline (Variable Context)

Because `scaffold-repo` acts as a fleet manager, building the variable context (what the Jinja templates actually see) happens in a deliberate, multi-stage process.

The engine resolves dependencies and layers data in this exact order **(where the last item wins and overwrites conflicts)**:

1. **Global Base Defaults:** The pre-loaded state from `.scaffold-defaults.yaml`.
2. **Stack Defaults:** The YAML from `stacks/<stack>/<stack_type>/.scaffold-defaults.yaml`.
3. **Template YAML:** The dependency archetype requested (e.g., `library-templates/cmake-c-git.yaml`).
4. **Profile YAML:** The organizational overrides (e.g., `profile: andy-curtis/andy-curtis`).
5. **License Profile YAML:** The base license terms derived from the profile.
6. **Local `scaffold.yaml`:** The repository's own local manifest. Any keys defined here permanently overwrite the inherited templates and profiles.

### YAML Inheritance (`includes`)
To keep your registry DRY (Don't Repeat Yourself), any YAML configuration file can inherit from one or more other YAML files using the `includes` array. When processed, the engine deeply merges the included files first, then applies the current file's variables as overrides.

**The Golden Rule:** Paths inside the `includes` array **must be the full path relative to the root of the `templates/` directory**.

**Example (`templates/profiles/andy-curtis/andy-curtis-knode.yaml`):**
```yaml
# 1. Inherit the base developer profile
includes:
  - profiles/andy-curtis/andy-curtis.yaml

# 2. Override the license for this specific context
license_profile: andy-curtis/knode-shared-19

# 3. Append additional data
contributors:
  knode:
    name: Knode.ai
    website: https://www.knodeai.com/
```

### Archetype Defaults (`root`)
When defining a dependency archetype (e.g., in `library-templates/cmake-c-git.yaml`), you are defining how to fetch and build a dependency. However, you also need to define the global workspace requirements for that archetype (like the default language or stack).

The engine uses a special `root` key in these files. Anything nested under `root` is unpacked directly into the top-level Jinja context for the repository.

**Example (`templates/library-templates/cmake-c-git.yaml`):**
```yaml
# ── Global Defaults (Unpacked into the main context) ──
root:
  stack: c/cmake
  language: C
  c_standard: 23

# ── The Template Payload (Kept scoped to the dependency) ──
kind: git
url: https://github.com/{{ github_project }}/{{ name }}.git
shallow: false
```

### Auto-Injected Variables
To save you from writing complex logic, the engine automatically calculates and injects derived variables into your Jinja context:
* `project_name`: The raw name (e.g., "my-python-app")
* `project_slug`: Hyphenated (e.g., "my-python-app")
* `project_snake`: Snake case (e.g., "my_python_app")
* `year`: The current year (or the explicitly configured `date`).
* Dependency arrays: Derived lists like `find_packages`, `link_libraries`, and `deps`.

---

## 4. Jinja Front-Matter & File Processing

Files ending in `.j2` are rendered as Jinja templates. Files without `.j2` are copied verbatim.

To tell the engine where a file *actually* belongs in the target repository, you use a special Jinja comment block at the very top of the `.j2` file:

```jinja
{#- scaffold-repo: { dest: "src/{{ project_snake }}/__main__.py", updatable: false } -#}
import sys
...
```

### Supported Front-Matter Attributes:
* **`dest` (string):** The absolute output path relative to the target repository root. *Note: Jinja variables like `{{ project_snake }}` are fully evaluated in the path!*
* **`context` (string):** Shifts the root of the Jinja variables. If `context: "cmake"`, then writing `{{ c_standard }}` evaluates to `cfg["cmake"]["c_standard"]`.
* **`header_managed` (boolean):** If `true`, the engine injects and manages SPDX headers in this file. (Defaults to true automatically for standard code files).
* **`executable` (boolean):** Ensures the resulting file has `+x` execution permissions (like `build.sh`).

### The Lifecycle Flags: `updatable` vs. `on_init`
The engine provides two specific flags to control *when* a template is evaluated. It is important to understand the difference:

* **`updatable` (boolean):** Defaults to `true`. If set to `false`, the engine will generate the file, but will politely skip overwriting it on future `--update` runs if the file already exists on disk.
    * *Use case:* A `src/main.py` file. You want to give the developer a "Hello World" starting point, but you never want the engine to overwrite their custom code later.
* **`on_init` (boolean):** Defaults to `false`. If set to `true`, the template is **strictly bound to the `--create` wizard**. During a standard `--update` across your fleet, the engine pretends this template does not even exist.
    * *Use case:* A bootstrap script, an initial database migration, or a Day-0 instructional text file. You only want these evaluated at the exact moment the repository is birthed, and you don't want the engine wasting cycles tracking them during routine fleet maintenance.

### Strict Mode Warning
The Jinja environment runs in **Strict Mode**. If you try to access `{{ lib.branch }}` and it wasn't defined, the engine will crash. **Always use `.get()` or `| default()` for optional values:**
```jinja
# Bad: Will crash if branch is missing
git clone --branch {{ lib.branch }} 

# Good: Safely handles missing data
git clone {% if lib.get('branch') %}--branch {{ lib.get('branch') }}{% endif %}
```

---

## 5. Package Routing & Prompts

You don't want every template rendering into every repository. The engine dynamically routes globs of files based on variable combinations defined in `.scaffold-defaults.yaml`.

```yaml
# templates/.scaffold-defaults.yaml
packages:
  base: true
  stack: true
  site: false

template_packages:
  base:
    - base/**
  stack:
    - stacks/{{ stack }}/base/**
    - stacks/{{ stack }}/{{ stack_type }}/**
  site:
    - mixins/jekyll-site/**
```

If a user configures a project with `stack: python/standard`, the engine seamlessly maps the `packages.stack` boolean to evaluate all templates inside `stacks/python/standard/**`. If the user adds `packages: { site: true }`, the `mixins/jekyll-site/` files are instantly included.

### Dynamic Prompts
Defaults files also declare interactive prompts to guide users during setup:
* **`init_prompts`:** Asked during `scaffold-repo --init` to configure the workspace-level `.scaffoldrc` environment variables.
* **`create_prompts`:** Asked during `scaffold-repo --create` to inject project-level variables directly into the new `scaffold.yaml`.

---

## 6. Sub-Applications (`app-resources/`)

C/C++ and Rust repositories often contain a base library *and* several sub-applications (like CLI tools, demos, or examples). `scaffold-repo` handles this natively via the `app-resources/` directory.

Any template placed in `app-resources/<stack>/<stack_type>/` is **exempt from standard root-level processing**. Instead, it is evaluated in a loop for *every* app defined in the local `scaffold.yaml`.

If a template exists at `app-resources/c/cmake/CMakeLists.txt.j2`, and the user's `scaffold.yaml` defines:
```yaml
apps:
  01_word_count:
    binaries:
      - src/main.c
```
The engine automatically routes the template to `01_word_count/CMakeLists.txt` and injects `app_project_name` into the context.

---

## 7. Defining OSS Licenses and Header Injection

Licenses are managed dynamically so you can update copyrights universally across your fleet. A license YAML file (e.g., `licenses/apache-2.0.yaml`) looks like this:

```yaml
spdx: |
  SPDX-FileCopyrightText: {{ year }} {{ author }}
  SPDX-License-Identifier: Apache-2.0
license: LICENSE
license_canonical: resources/licenses/Apache-2.0.txt
notice: |
  {{ project_title }} © {{ year }} {{ author }}
  Licensed under Apache-2.0 (see LICENSE)
```

### Automatic Comment Syntax Translation
You only define the `spdx` block **once** in plain text. When `scaffold-repo` runs, it scans file extensions and automatically wraps the rendered SPDX text in the correct comment syntax for that language (e.g., `//` for C/C++, `#` for Python/Bash, `--` for SQL).

### Multi-License Projects
If specific files in your repository require different licenses (e.g., a borrowed sorting algorithm), you can override specific file globs directly in your `scaffold.yaml`:

```yaml
license_overrides:
  "src/third_party/**": andy-curtis/dutch-national-flag
```
The engine will apply your primary headers to 99% of your files, but seamlessly apply the Dutch National Flag headers to the files in `third_party/`, and dynamically append its required `NOTICE` block to your repository's root `NOTICE` file!