"""Allow scheduled backup only if the usual run did not succeed or start recently."""
import datetime as dt
import json, os, subprocess


def needs_backup(runs, own_id, current_time):
    for run in runs:
        if str(run.get('id')) == str(own_id):
            continue
        try:
            created = dt.datetime.fromisoformat(run['created_at'].replace('Z', '+00:00'))
            age = (current_time - created).total_seconds()
        except (ValueError, KeyError, TypeError):
            continue
        if not (0 <= age < 14 * 60):
            continue
        if run.get('status') != 'completed':
            return False
        if run.get('event') == 'workflow_dispatch' and run.get('conclusion') == 'success':
            return False
    return True


def main():
    run = True
    if os.environ.get('GITHUB_EVENT_NAME') == 'schedule':
        try:
            repo = os.environ['GITHUB_REPOSITORY']
            endpoint = f'repos/{repo}/actions/workflows/news.yml/runs?per_page=30'
            result = subprocess.run(['gh', 'api', endpoint], check=True, capture_output=True, text=True, timeout=20)
            runs = json.loads(result.stdout)['workflow_runs']
            run = needs_backup(runs, os.environ.get('GITHUB_RUN_ID'), dt.datetime.now(dt.timezone.utc))
        except Exception as e:
            # Fail closed: do not send duplicates if we cannot verify the latest run.
            print('Could not verify recent runs; skipping backup:', type(e).__name__)
            run = False
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
        f.write('run=' + ('true' if run else 'false') + '\n')
    print('Backup decision:', 'run' if run else 'skip')

if __name__ == '__main__':
    main()
