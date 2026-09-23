# Custom Agent Skills

This repository stores custom Agent Skills in the `skills/` directory.
Each skill must contain a `SKILL.md` with YAML frontmatter containing `name`
and `description`.

## Install from GitHub

Push this repository to GitHub, then install one skill:

```bash
npx skills add <github-owner>/<repository> --skill <skill-name>
```

Install all discovered skills:

```bash
npx skills add <github-owner>/<repository> --all
```

List skills without installing:

```bash
npx skills add <github-owner>/<repository> --list
```

## Add another skill

```bash
npx skills init skills/my-skill
```

Or create `skills/<skill-name>/SKILL.md` manually, then commit the change.
