# `scaffold-repo`

Managing one C/C++ repository is easy. Managing twenty interconnected micro-repos—keeping their CMake configurations, OSS licenses, and Git workflows synchronized—is a nightmare.

`scaffold-repo` is a fleet manager and build orchestrator for your code. Instead of manually updating 20 different `CMakeLists.txt` files or tracking down missing dependencies, you define your entire ecosystem of small repositories in a centralized YAML registry. With a single CLI command, `scaffold-repo` resolves the dependency graph, injects standard build scripts, enforces license compliance, and orchestrates your Git branching across the entire fleet.

### The 10-Second Overview

**1. Define your fleet in YAML:**
```yaml
project_title: A Map Reduce Library
version: 0.0.3
template: cmake-c-git
depends_on:
  - the-io-library
  - a-memory-library
```

**2. Sync and Build everything with one command:**
```bash
scaffold-repo a-map-reduce-library --build-deps --build
```
*`scaffold-repo` instantly clones the missing dependencies, topologically sorts them, builds them, generates the updated `CMakeLists.txt` for your Map Reduce library, and compiles your code.*

---

## 🚀 Core Features

* **Instant Dependency Resolution:** Never fight missing packages again. The engine reads your YAML `depends_on` arrays, topologically sorts the graph, and automatically clones, configures, and builds external Git dependencies (like `libuv` or your own internal libraries) in the exact required order.
* **Write Once, Update Everywhere:** Need to bump your minimum CMake version or standardize a Dockerfile? Update the central Jinja template once, run `--update`, and the engine intelligently syncs the changes across your entire fleet without overwriting manually protected code.
* **Standardized C/C++ Builds:** Automatically generates `CMakeLists.txt` and `build.sh` scripts that produce four standard variants (`debug`, `memory`, `static`, `shared`) simultaneously, plus an umbrella target (`<name>::<name>`) for dead-simple downstream linking.
* **Automated OSS Compliance:** Automatically injects and updates SPDX license headers across C, C++, Python, JS, and HTML files, and aggregates `NOTICE` files based on repository lineage.

---

## 🛠 The GitOps Workflow

`scaffold-repo` isn't just a build tool; it manages your development lifecycle. It enforces a clean **`main` -> `dev-*` -> `feat/*`** branching topology across your fleet.

**1. Start a Feature**
```bash
scaffold-repo my-namespace --start-feature "dynamic-resize"
```
> Clones missing repositories, checks out the current integration branch (e.g., `dev-v0.0.2`), and branches `feat/dynamic-resize`.

**2. Compile & Iterate**
```bash
scaffold-repo my-namespace --build-deps --build
```
> Fetches and compiles all dependencies into a flat sandbox, then executes `./build.sh build` on your local code.

**3. Commit and Push**
```bash
scaffold-repo my-namespace --commit "WIP: integrating allocator" --push
```
> Commits changes to the feature branch and pushes to origin (safely blocking direct commits to `main` or `dev-*`).

**4. Publish Release**
```bash
scaffold-repo my-namespace --publish-release --push
```
> After merging your PRs into the `dev` branch, this command checks out `main`, merges the `dev` branch, auto-tags the release based on your YAML version, bumps the YAML version for the next cycle, and pushes.

---

## 🏗 Under the Hood: Registry Architecture

*(Keep the rest of your original README starting from the "Registry Architecture" section here, as it acts as the detailed manual for users who are already hooked.)*

* **Registry Architecture** (Tree view of `/templates`)
* **Declarative Configuration** (Deep dive into `apps` and `tests`)
* **Complete CLI Reference**
* **Advanced Template Configuration** (Jinja front-matter, etc.)
