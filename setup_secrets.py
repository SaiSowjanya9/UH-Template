"""One-time helper: prompts for the hosted keys and writes them into .env.

Run:  .venv\\Scripts\\python.exe setup_secrets.py

Paste is visible so you can check it. Values already set are kept unless
you type a new one. Delete this file once hosting is working.
"""

ENV = ".env"

PROMPTS = [
    ("SUPABASE_SERVICE_KEY",
     "Supabase -> Project Settings -> API Keys -> Secret keys -> default -> copy",
     ("sb_secret_", "eyJ")),
    ("SUPABASE_ANON_KEY",
     "Supabase -> Project Settings -> API Keys -> Publishable key -> default -> copy",
     ("sb_publishable_", "eyJ")),
    ("RENDER_API_KEY",
     "Render -> Account Settings -> API Keys -> Create API key",
     ("rnd_",)),
]


def main():
    lines = open(ENV, encoding="utf-8").read().splitlines()
    values = {}
    for line in lines:
        if "=" in line:
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()

    changed = False
    for key, where, prefixes in PROMPTS:
        if values.get(key):
            print(f"{key}: already set, keeping it (press Enter to keep)")
        print(f"{key}\n  get it: {where}")
        raw = input("  paste value (Enter to skip): ").strip().strip("'\"")
        if not raw:
            continue
        if not raw.startswith(prefixes):
            print(f"  WARNING: does not start with {'/'.join(prefixes)} - saving anyway")
        values[key] = raw
        changed = True
        print(f"  saved ({len(raw)} chars)")

    out = []
    for line in lines:
        k = line.split("=", 1)[0].strip() if "=" in line else ""
        out.append(f"{k}={values[k]}" if k in values else line)
    if changed:
        open(ENV, "w", encoding="utf-8").write("\n".join(out) + "\n")
        print("Wrote .env")
    else:
        print("Nothing changed.")

    missing = [k for k, _, _ in PROMPTS if not values.get(k)]
    print("Still missing: " + ", ".join(missing) if missing else "All three keys are in .env.")


if __name__ == "__main__":
    main()
