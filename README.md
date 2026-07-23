
# archived once I realized they already do exactly this [here](https://duckbench.com/generate) 😅 


# Ducky Bench additions

A tiny runner for trying OpenRouter models on Ducky Bench's public SVG-image tasks.

You pick the models in `models.toml`, run the workflow, and get a review page plus a ZIP of the results. Nothing here posts votes or changes the public leaderboard.

## The easy route: run it from GitHub

1. In this repo, go to **Settings → Secrets and variables → Actions**.
2. Add a repository secret named `OPENROUTER_API_KEY`.
3. Open the **Actions** tab, choose **Run Ducky Bench**, then click **Run workflow**.
4. When it finishes, download the run artifact. It contains the SVGs, a side-by-side `review.html`, and `submission-bundle.zip`.

The workflow is manual only. Pushing a config edit does not call any models.

## Pick models

Open [`models.toml`](models.toml). Every named model starts with:

~~~toml
enabled = false
~~~

Change that to `true` for the handful you want. The file is grouped into cheap vision picks, small same-family comparisons, and a requested text-model catalog.

The default also has:

~~~toml
[free_vision]
enabled = true
~~~

That means each real run adds every *currently free* OpenRouter model that can read an image. It skips Gemma and Venice models, plus embeddings, guardrails, and rerankers. Set it to `false` if you want only the models you explicitly enabled.

This benchmark needs image input. Before calling OpenRouter, the runner checks the live model list and skips text-only or retired IDs rather than spending money on a request that cannot work.

## Keep the cost boring

`models.toml` starts with a $5 reported-cost ceiling for paid models:

~~~toml
max_total_cost_usd = 5.00
~~~

Free models do not count toward it. Lower it, raise it, or use `0` to turn the ceiling off. The runner still needs a real OpenRouter key for free models.

## Running it on your machine

Python 3.11+ is enough—there are no packages to install.

~~~bash
git clone https://github.com/cheeseonamonkey/Ducky-Bench-additions.git
cd Ducky-Bench-additions
cp .env.example .env
~~~

Put your key in `.env`, make your model choices, then check the plan:

~~~bash
python3 run.py
~~~

Run it for real:

~~~bash
python3 run.py --execute --run-name first-try
~~~

If a run stops halfway through, resume it without redoing valid outputs:

~~~bash
python3 run.py --execute --run-name first-try --resume
~~~

## What you get

Each run creates an ignored folder under `runs/` with:

- the public reference images
- generated SVGs and raw model responses
- `review.html` for quick side-by-side checking
- `manifest.json` with the exact IDs, prompt, and costs
- `submission-bundle.zip`

## One honest caveat

This makes reproducible candidate outputs, not official Ducky Bench Elo. The public site lets people vote but does not expose a public model-import API or its complete original generation setup. If you want an official addition, send the maintainer the ZIP along with the exact model IDs and settings.
