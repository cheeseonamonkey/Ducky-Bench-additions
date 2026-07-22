# Ducky Bench additions

Run a small, reproducible set of vision-capable models against Ducky Bench's three public SVG-reference tasks. You ordinarily edit only models.toml; API secrets belong in local .env, never in Git.

This project generates candidate SVGs, the source/config metadata, a side-by-side review page, and a ZIP submission bundle. It does not insert models or votes into Ducky Bench's public leaderboard—the site exposes voting, not a public model-submission API. Confirm the benchmark maintainer's exact prompt/settings before calling results official.

## Quick start

Requires Python 3.11+; no packages to install.

~~~bash
git clone https://github.com/cheeseonamonkey/Ducky-Bench-additions.git
cd Ducky-Bench-additions
cp .env.example .env
~~~

1. Put an API key in .env.
2. Edit models.toml: enable and name your chosen models; keep them vision-capable.
3. Inspect the no-cost plan:

~~~bash
python3 run.py
~~~

4. Run it deliberately:

~~~bash
python3 run.py --execute --run-name first-ten
~~~

The run requires three requests per enabled model. Ten models = 30 API calls. The default config stops starting new calls after the provider-reported spend reaches $10; change max_total_cost_usd if desired.

## Config

models.toml uses an OpenAI-compatible Chat Completions endpoint, defaulting to OpenRouter. Each model has:

~~~toml
[[models]]
label = "My favorite model"
id = "provider/model-id"
enabled = true
request = { temperature = 0.10, max_tokens = 8000 }
~~~

For a per-model endpoint/key, add:

~~~toml
endpoint = "https://provider.example/v1/chat/completions"
api_key_env = "PROVIDER_API_KEY"
~~~

Extra request fields are passed through verbatim, so provider-specific fields such as reasoning = { effort = "high" } work when supported.

## Outputs

Each execution writes an ignored directory under runs/<run-name>/:

~~~text
references/             downloaded public target images
outputs/<model>/        validated SVG outputs
responses/<model>/      raw API responses
models.toml             frozen configuration snapshot
manifest.json           model IDs, prompt, sources, artifacts, reported spend
review.html             local visual comparison page
submission-bundle.zip   compact handoff package
~~~

Use a stable name plus --resume to reuse valid finished SVGs after an interruption:

~~~bash
python3 run.py --execute --run-name first-ten --resume
~~~

You can restrict a retry to a model or test:

~~~bash
python3 run.py --execute --run-name first-ten --resume --model "My favorite model" --test 2
~~~

## Protocol caveat

The public [Ducky Bench voting page](https://ducky-bench.joinity.site/index.php?test=1) says models recreate raster references as SVGs. Its page does not disclose its full original generation protocol or provide a model-import endpoint. This runner uses the public target images and records its own explicit prompt/settings so the work is repeatable—but it cannot establish comparable official Elo by itself.

For an official addition, send the maintainer the generated bundle plus exact model/provider IDs and settings, then ask them to import the outputs and create the pairings.

