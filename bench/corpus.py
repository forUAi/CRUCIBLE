"""
CRUCIBLE :: bench/corpus.py

The pinned external corpus, and its partitions.

Selection rules, applied before anything was measured:

  * Every language band has both *applications* and *negative controls* --
    framework repositories that must not be mistaken for something you
    deploy. A corpus of only deployable things cannot detect the failure
    mode where everything looks deployable.
  * Monorepos, frontend/backend pairs, nested modules and repositories whose
    root runs nothing are included on purpose. Those are the shapes the
    single-root assumption got wrong.
  * Healthy, maintained, permissively licensed, and pinned to an exact SHA so
    a score is reproducible.

Partitions are assigned by a hash of the repository slug, not by hand, so
they cannot drift toward whatever makes a number look better:

    dev         tune freely
    validation  check generalisation while iterating
    holdout     CONSUMED at checkpoint 1. It was run once, it disagreed on
                prometheus, and that disagreement was adjudicated into a
                label change -- so it has influenced the product and can no
                longer measure generalisation. Its result stands as recorded;
                it is not a clean holdout any more.
    sealed      LOCKED for the next checkpoint. Never run, never inspected.
                wsbench refuses it outright, with no --unlock escape.

`expect` records only what can be asserted with confidence from the
repository's own declarations. Where the honest answer is "a human would have
to decide", the field is None and that repository is excluded from that
metric rather than being given a guessed label. Inventing labels to raise a
score is the same defect as changing them afterwards.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Repo:
    slug: str
    url: str
    band: str                          # python | java | go | node
    shape: str                         # app | framework | monorepo | nested | pair | tool
    sha: str = ""                      # pinned at fetch time
    # ---- ground truth; None means "not confidently labelled" ----
    languages: list[str] = field(default_factory=list)
    root_runnable: Optional[bool] = None
    min_runnable: Optional[int] = None      # at least this many components
    max_runnable: Optional[int] = None      # at most this many
    must_include: list[str] = field(default_factory=list)
    must_exclude: list[str] = field(default_factory=list)
    rationale: str = ""

    sealed: bool = False               # reserved for the next checkpoint

    @property
    def split(self) -> str:
        if self.sealed:
            return "sealed"
        h = int(hashlib.sha256(self.slug.encode()).hexdigest()[:8], 16)
        return ("dev", "validation", "holdout")[h % 3]


# Checkpoint 1's holdout is consumed: it ran, it disagreed on prometheus, and
# the adjudication changed a label. Recording that here rather than in a
# commit message, because the next person to read a holdout number needs to
# know which one they are looking at.
HOLDOUT_CONSUMED = {
    "checkpoint": 1,
    "result": "6/6 scored, root_runnable 4/5 (80%), all other checks 100%",
    "why_consumed": "the prometheus disagreement was adjudicated into a "
                    "corrected label, so the split influenced the product",
}


# --------------------------------------------------------------------------
# 32 repositories: 8 per band.
# --------------------------------------------------------------------------

CORPUS: list[Repo] = [
    # ---------------- Python ----------------
    Repo("psf/requests", "https://github.com/psf/requests", "python", "framework",
         languages=["python"], root_runnable=False, max_runnable=0,
         rationale="the canonical HTTP library; must never read as an app"),
    Repo("pallets/flask", "https://github.com/pallets/flask", "python", "framework",
         languages=["python"], root_runnable=False, max_runnable=0,
         rationale="framework source whose examples/ depend on flask"),
    Repo("django/django", "https://github.com/django/django", "python", "framework",
         languages=["python"], root_runnable=False, max_runnable=0,
         rationale="framework; no manage.py and no application in the tree"),
    Repo("Textualize/rich", "https://github.com/Textualize/rich", "python", "framework",
         languages=["python"], root_runnable=False, max_runnable=0,
         rationale="library that mentions web frameworks in prose"),
    Repo("fastapi/full-stack-fastapi-template",
         "https://github.com/fastapi/full-stack-fastapi-template", "python", "pair",
         languages=["python", "node"], root_runnable=False, min_runnable=2,
         must_include=["backend", "frontend"],
         rationale="frontend/backend pair; root is not the application"),
    Repo("netbox-community/netbox", "https://github.com/netbox-community/netbox",
         "python", "app", languages=["python"], root_runnable=False, min_runnable=1,
         rationale="Django app whose manage.py is at netbox/netbox/, so the repository root is not the application"),
    Repo("apache/airflow", "https://github.com/apache/airflow", "python", "monorepo",
         languages=["python"], root_runnable=False,
         rationale="multi-package repository; the root is not the application"),
    Repo("pypa/pipenv", "https://github.com/pypa/pipenv", "python", "tool",
         languages=["python"], min_runnable=1,
         rationale="CLI tool with a declared console entry point"),

    # ---------------- Java ----------------
    Repo("spring-projects/spring-petclinic",
         "https://github.com/spring-projects/spring-petclinic", "java", "app",
         languages=["java"], root_runnable=True, min_runnable=1,
         rationale="single-module Spring Boot service; the known-good case"),
    Repo("spring-projects/spring-framework",
         "https://github.com/spring-projects/spring-framework", "java", "framework",
         languages=["java"], root_runnable=False,
         rationale="negative control: gradle multi-project framework source"),
    Repo("spring-guides/gs-rest-service",
         "https://github.com/spring-guides/gs-rest-service", "java", "nested",
         languages=["java"], root_runnable=False, min_runnable=1,
         rationale="initial/ and complete/ subprojects; root runs nothing"),
    Repo("spring-guides/gs-accessing-data-jpa",
         "https://github.com/spring-guides/gs-accessing-data-jpa", "java", "nested",
         languages=["java"], root_runnable=False,
         rationale="same shape, JPA variant"),
    Repo("apache/dubbo", "https://github.com/apache/dubbo", "java", "monorepo",
         languages=["java"], root_runnable=False,
         rationale="large maven reactor; root is an aggregator"),
    Repo("google/guava", "https://github.com/google/guava", "java", "framework",
         languages=["java"], root_runnable=False,
         rationale="negative control: pure library reactor"),
    # LABEL CORRECTION: micronaut-examples was labelled java/monorepo. The
    # repository is archived and its default branch contains only README.md,
    # so the label was factually wrong about the repository -- CRUCIBLE
    # reporting zero workspaces was correct. Replaced with a Maven reactor
    # that still has code, rather than scoring against an empty tree.
    Repo("apache/camel", "https://github.com/apache/camel", "java", "monorepo",
         languages=["java"], root_runnable=False,
         rationale="large maven reactor; replaces the archived, empty "
                   "micronaut-examples"),
    Repo("quarkusio/quarkus-quickstarts",
         "https://github.com/quarkusio/quarkus-quickstarts", "java", "monorepo",
         languages=["java"], root_runnable=False,
         rationale="many independent quickstart modules"),

    # ---------------- Go ----------------
    Repo("gin-gonic/gin", "https://github.com/gin-gonic/gin", "go", "framework",
         languages=["go"], root_runnable=False, max_runnable=0,
         rationale="negative control: the framework itself"),
    Repo("grpc/grpc-go", "https://github.com/grpc/grpc-go", "go", "nested",
         languages=["go"], root_runnable=False,
         must_exclude=["interop/observability", "interop/xds"],
         rationale="ten nested modules, no go.work; mains live in interop/"),
    # ADJUDICATED after the first holdout run disagreed. The label was wrong.
    # It conflated "the root PACKAGE is a library" (true: no .go at the top
    # level) with "the root WORKSPACE is not runnable" (false). The author's
    # own go.work lists "." as a member, and the Makefile builds cmd/prometheus
    # and cmd/promtool from that module. CRUCIBLE's unit is the module, and its
    # documented Go rule -- entry point at the module root or under cmd/ -- is
    # standard Go layout. Corrected on that evidence, not on the score.
    Repo("prometheus/prometheus", "https://github.com/prometheus/prometheus",
         "go", "app", languages=["go"], root_runnable=True, min_runnable=1,
         must_include=["."],
         rationale="go.work declares '.'; the Makefile builds cmd/prometheus "
                   "and cmd/promtool from the root module"),
    Repo("go-gitea/gitea", "https://github.com/go-gitea/gitea", "go", "app",
         languages=["go"], root_runnable=True, min_runnable=1, must_include=["."],
         rationale="main.go at the module root"),
    Repo("heroku/go-getting-started", "https://github.com/heroku/go-getting-started",
         "go", "app", languages=["go"], root_runnable=True, min_runnable=1,
         rationale="small runnable Go web app; known-good execution case"),
    Repo("spf13/cobra", "https://github.com/spf13/cobra", "go", "framework",
         languages=["go"], root_runnable=False,
         rationale="negative control: CLI library, not a CLI"),
    Repo("kubernetes/client-go", "https://github.com/kubernetes/client-go",
         "go", "framework", languages=["go"], root_runnable=False,
         rationale="negative control: client library with examples/"),
    Repo("hashicorp/consul", "https://github.com/hashicorp/consul", "go", "monorepo",
         languages=["go"], root_runnable=True, min_runnable=1, must_include=["."],
         rationale="main.go at the module root, plus nested modules"),

    # ---------------- Node / TypeScript ----------------
    Repo("expressjs/express", "https://github.com/expressjs/express", "node", "framework",
         languages=["node"], root_runnable=False, max_runnable=0,
         rationale="negative control: the framework itself"),
    Repo("nestjs/nest", "https://github.com/nestjs/nest", "node", "framework",
         languages=["node"], root_runnable=False, max_runnable=0,
         rationale="negative control: framework monorepo of packages"),
    Repo("backstage/backstage", "https://github.com/backstage/backstage", "node",
         "monorepo", languages=["node"], root_runnable=False, min_runnable=1,
         must_exclude=["."],
         rationale="yarn workspaces; root delegates via backstage-cli repo start"),
    Repo("vercel/turborepo", "https://github.com/vercel/turborepo", "node", "monorepo",
         languages=["node", "rust"], root_runnable=False, min_runnable=1,
         rationale="pnpm workspace with negation, plus a Rust CLI beside it"),
    Repo("heroku/node-js-getting-started",
         "https://github.com/heroku/node-js-getting-started", "node", "app",
         languages=["node"], root_runnable=True, min_runnable=1,
         rationale="small runnable Express app; known-good execution case"),
    Repo("directus/directus", "https://github.com/directus/directus", "node",
         "monorepo", languages=["node"], root_runnable=False,
         rationale="pnpm monorepo with an api package"),
    Repo("nrwl/nx", "https://github.com/nrwl/nx", "node", "monorepo",
         languages=["node"], root_runnable=False,
         rationale="Nx workspace; the tool's own repository"),
    # LABEL CORRECTION: this carried max_runnable=0. That was wrong about the
    # repository: packages-private/sfc-playground is `private: true` with
    # `dev`/`serve` scripts -- a genuinely runnable playground app. "Framework
    # repository" and "contains nothing runnable" are different claims and I
    # had conflated them. The claim that matters for a negative control is
    # that the ROOT is not a service, which is what is asserted now.
    Repo("vuejs/core", "https://github.com/vuejs/core", "node", "framework",
         languages=["node"], root_runnable=False, must_exclude=["."],
         rationale="negative control: pnpm framework monorepo; root deploys "
                   "nothing, though it does contain runnable playgrounds"),
]


# --------------------------------------------------------------------------
# SEALED for checkpoint 2. Chosen to cover the same shapes as the consumed
# holdout -- an application, a framework negative control, a monorepo and a
# nested-module case in each band -- and deliberately NOT looked at. No
# labels beyond language and shape are recorded yet, because writing a
# ground-truth label requires opening the repository.
# --------------------------------------------------------------------------

SEALED: list[Repo] = [
    Repo("tiangolo/sqlmodel", "https://github.com/tiangolo/sqlmodel",
         "python", "framework", sealed=True, languages=["python"],
         rationale="sealed: python library negative control"),
    Repo("Pylons/pyramid", "https://github.com/Pylons/pyramid",
         "python", "framework", sealed=True, languages=["python"],
         rationale="sealed: python web framework negative control"),
    Repo("saleor/saleor", "https://github.com/saleor/saleor",
         "python", "app", sealed=True, languages=["python"],
         rationale="sealed: large Django application"),
    Repo("quarkusio/quarkus", "https://github.com/quarkusio/quarkus",
         "java", "monorepo", sealed=True, languages=["java"],
         rationale="sealed: very large maven reactor"),
    Repo("micronaut-projects/micronaut-core",
         "https://github.com/micronaut-projects/micronaut-core",
         "java", "framework", sealed=True, languages=["java"],
         rationale="sealed: gradle multi-project framework"),
    Repo("jenkinsci/jenkins", "https://github.com/jenkinsci/jenkins",
         "java", "app", sealed=True, languages=["java"],
         rationale="sealed: maven multi-module application"),
    Repo("etcd-io/etcd", "https://github.com/etcd-io/etcd",
         "go", "nested", sealed=True, languages=["go"],
         rationale="sealed: go.work with several nested modules"),
    Repo("cli/cli", "https://github.com/cli/cli", "go", "app", sealed=True,
         languages=["go"], rationale="sealed: Go CLI with cmd/ layout"),
    Repo("uber-go/zap", "https://github.com/uber-go/zap", "go", "framework",
         sealed=True, languages=["go"],
         rationale="sealed: Go library negative control"),
    Repo("vitejs/vite", "https://github.com/vitejs/vite", "node", "monorepo",
         sealed=True, languages=["node"],
         rationale="sealed: pnpm monorepo of packages"),
    Repo("strapi/strapi", "https://github.com/strapi/strapi", "node",
         "monorepo", sealed=True, languages=["node"],
         rationale="sealed: yarn monorepo with an application"),
    Repo("fastify/fastify", "https://github.com/fastify/fastify", "node",
         "framework", sealed=True, languages=["node"],
         rationale="sealed: node framework negative control"),
]

CORPUS.extend(SEALED)


def by_split(split: str) -> list[Repo]:
    return [r for r in CORPUS if r.split == split]


def summary() -> str:
    lines = [f"{len(CORPUS)} repositories"]
    for split in ("dev", "validation", "holdout"):
        rs = by_split(split)
        bands = {}
        for r in rs:
            bands[r.band] = bands.get(r.band, 0) + 1
        lines.append(f"  {split:11} {len(rs):2}  " +
                     ", ".join(f"{k}={v}" for k, v in sorted(bands.items())))
    for band in ("python", "java", "go", "node"):
        rs = [r for r in CORPUS if r.band == band]
        shapes = {}
        for r in rs:
            shapes[r.shape] = shapes.get(r.shape, 0) + 1
        lines.append(f"  {band:11} {len(rs):2}  " +
                     ", ".join(f"{k}={v}" for k, v in sorted(shapes.items())))
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary())
