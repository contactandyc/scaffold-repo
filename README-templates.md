# Authoring Templates for `scaffold-repo`

The `scaffold-repo` Python CLI is purely an execution engine. It contains no hardcoded opinions about your build system, your CI/CD pipeline, or your folder structures. All of that power is defined entirely within your **Template Registry**.

A Template Registry is a directory (often distributed as its own Git repository) containing YAML configurations, Jinja2 templates, and licensing rules. This guide explains how to write your own templates, define custom build flavors, and harness the engine's dynamic routing logic.

---

## 1. The Registry Directory Structure

The engine expects a specific directory layout within your registry to resolve inheritance and routing properly:

```text
templates/
├── .scaffold-defaults.yaml    # Global router: defines packages & file globs
├── profiles/                  # Base YAML configs (e.g., author info, base dev tools)
├── library-templates/         # Archetype YAML configs (e.g., cmake-c-git.yaml)
├── libraries/                 # The global library index (depends_on, version, pkg-config)
├── licenses/                  # SPDX and NOTICE file definitions
├── flavors/                   # Primary Jinja templates for root projects (e.g., build.sh)
├── mixins/                    # Modular Jinja templates (docs, CI/CD, linters)
├── app-resources/             # Special templates looped per sub-application
└── resources/                 # Global assets like aliases.yaml or canonical license texts
```

---

## 2. The Data Merge Pipeline (Variable Context)

Because `scaffold-repo` acts as a fleet manager, building the variable context (what the Jinja templates actually see) happens in a deliberate, two-stage process.

### Stage 1: Global Pre-Loading (The Fleet Context)
Before rendering anything, the engine reads the *entire* registry into memory.
1. It loads `.scaffold-defaults.yaml`.
2. It discovers and pre-loads all `profiles/`, `libraries/`, and `apps/` into a global index.
   *(This allows the engine to instantly resolve a local declaration like `depends_on: my-other-library` without having to blindly clone `my-other-library` just to read its config).*

### Stage 2: Target Resolution (The Active Repository)
Once the engine knows which specific repository it is scaffolding, it builds the localized variable context. It resolves dependencies and layers data in this exact order **(where the last item wins and overwrites conflicts)**:

1. **The Global Base:** The pre-loaded state from Stage 1.
2. **Template YAML:** The archetype requested (e.g., `template: cmake-c-git` pulls in `library-templates/cmake-c-git.yaml`).
3. **Profile YAML:** The organizational overrides (e.g., `profile: my-org/my-profile`).
4. **License Profile YAML:** The base license terms (e.g., `license_profile: my-org/mit`).
5. **Local `scaffold.yaml`:** The repository's own local manifest. Any keys defined here permanently overwrite the inherited templates and profiles.

### Auto-Injected Variables
To save you from writing complex logic, the engine automatically calculates and injects several derived variables into your Jinja context:
* `project_name`: The raw name (e.g., "A Memory Library")
* `project_slug`: Hyphenated (e.g., "a-memory-library")
* `cmake_project`: Snake case (e.g., "a_memory_library")
* `project_camel`: Camel case (e.g., "AMemoryLibrary")
* `year`: The current year (or the explicitly configured `date`).
* `cmake.libraries`: A topologically sorted list of all dependencies required by this specific repo.
* `cmake.find_packages`: A flattened list of all CMake `find_package` requirements.
* `cmake.link_libraries`: A flattened list of all CMake `target_link_libraries` requirements.

---

## 3. Jinja Front-Matter & File Processing

Files ending in `.j2` are rendered as Jinja templates. Files without `.j2` are copied verbatim.

By default, a file like `flavors/cmake-c/build.sh.j2` would be written to exactly that path: `flavors/cmake-c/build.sh`. To tell the engine where the file *actually* belongs in the target repository, you use a special Jinja comment block at the very top of the `.j2` file:

```jinja
{#- scaffold-repo: { dest: "build.sh", updatable: true, executable: true } -#}
#!/usr/bin/env bash
...
```

### Supported Front-Matter Attributes:
* **`dest` (string):** The absolute output path relative to the target repository root (e.g., `src/main.c`).
* **`updatable` (boolean):** If `false`, the engine will generate the file the *first* time it runs, but will skip it on future `--update` runs. This is perfect for initial `src/main.c` or `.gitignore` files where developers will add their own manual edits over time.
* **`context` (string):** Shifts the root of the Jinja variables. If `context: "cmake"`, then writing `{{ c_standard }}` evaluates to `cfg["cmake"]["c_standard"]`.
* **`header_managed` (boolean):** If `true`, the engine expects to inject and manage SPDX headers in this file. (Defaults to true automatically for `.c`, `.h`, `.py`, `.sql`, etc.).
* **`executable` (boolean):** If `true`, ensures the resulting file has `+x` execution permissions (like `build.sh`). *Note: The engine also automatically detects if the source template in the registry is marked as executable on disk.*

### Strict Mode Warning
The Jinja environment runs in **Strict Mode**. If you try to access `{{ lib.branch }}` and it wasn't defined in the YAML, the engine will crash. **Always use `.get()` or `| default()` for optional values:**
```jinja
# Bad: Will crash if branch is missing
git clone --branch {{ lib.branch }} 

# Good: Safely handles missing data
git clone {% if lib.get('branch') %}--branch {{ lib.get('branch') }}{% endif %}
```

---

## 4. Package Routing (`.scaffold-defaults.yaml`)

You don't want every template rendering into every repository. The engine uses `.scaffold-defaults.yaml` to route globs of files based on boolean toggles.

```yaml
# templates/.scaffold-defaults.yaml
packages:
  base: true         # Always on
  flavor: true       # Always on
  docs: false        # Off by default, can be toggled in scaffold.yaml

template_packages:
  base:
    - base/**
  flavor:
    - flavors/{{ repo_flavor }}/**
  docs:
    - mixins/markdown-docs/**
```

If a user writes `repo_flavor: cmake-c` in their local `scaffold.yaml`, the engine evaluates all templates inside `flavors/cmake-c/**`. If they add `packages: { docs: true }`, the `mixins/markdown-docs/` files are dynamically included in the generation cycle.

---

## 5. Sub-Applications (`app-resources/`)

Repositories often contain a base library *and* several sub-applications (like CLI tools, demos, or examples). `scaffold-repo` handles this natively via the `app-resources/` directory.

Any template placed in `app-resources/<flavor>/` is **exempt from standard root-level processing**. Instead, it is evaluated in a loop for *every* app defined in the local `scaffold.yaml`.

**Example local `scaffold.yaml`:**
```yaml
apps:
  context:
    dest: examples   # Base output folder
  01_word_count:
    binaries:
      - src/main.c
```

If you have a template at `app-resources/cmake-c/CMakeLists.txt.j2`:
1. The engine loops over `01_word_count`.
2. It calculates the output directory: `examples/01_word_count/`.
3. It renders the template and places it at `examples/01_word_count/CMakeLists.txt`.
4. It injects `app_project_name` (e.g., `Project_01_word_count`) directly into the Jinja context.

---

## 6. Defining OSS Licenses and Header Injection

Licenses are managed dynamically so you can update copyrights universally across your fleet. A license YAML file (e.g., `licenses/mit.yaml`) looks like this:

```yaml
spdx: |
  SPDX-FileCopyrightText: {{ year }} {{ author }}
  SPDX-License-Identifier: MIT
license: LICENSE
license_canonical: resources/licenses/MIT.txt
notice: |
  {{ project_title }} © {{ year }} {{ author }}
  Licensed under MIT (see LICENSE)
```

### Automatic Comment Syntax Translation
You only define the `spdx` block **once** in plain text. When `scaffold-repo` runs, it scans the file extensions in your repository and automatically wraps the rendered SPDX text in the correct comment syntax for that language.

If the engine evaluates the MIT YAML shown above for the year 2026, here is how it gets injected into your repository:

**In a C/C++ file (`src/main.c` or `include/header.h`):**
```c
// SPDX-FileCopyrightText: 2026 Your Name
// SPDX-License-Identifier: MIT

#include <stdio.h>
int main() {
    return 0;
}
```

**In a Python or Bash script (`build.sh` or `script.py`):**
```python
#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Your Name
# SPDX-License-Identifier: MIT

import sys
```

**In a SQL file (`schema.sql`):**
```sql
-- SPDX-FileCopyrightText: 2026 Your Name
-- SPDX-License-Identifier: MIT

CREATE TABLE users (id INT);
```

### Multi-License Projects
If specific files in your repository require different licenses (e.g., you borrowed a sorting algorithm that uses BSD-2), you don't have to change the whole project's primary license. You can override specific file globs directly in your `scaffold.yaml`:

```yaml
license_overrides:
  "src/third_party/**": my-org/bsd-2
```
The engine will apply the MIT headers to 99% of your files, but seamlessly apply the BSD-2 headers to the files residing in the `third_party` directory. Furthermore, the `NOTICE` block from the BSD-2 profile will automatically be appended to the root `NOTICE` file alongside your primary license!
