"""Collect configured evidence and append backend feedback to Gradescope results.

This module and local_client.py use only the standard library. Assignment-specific
file and report selection is read from the backend at the start of each run.
"""

import argparse
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import hashlib
import html
import json
import math
from pathlib import Path, PurePosixPath
import re
import sys
import textwrap
import time
import uuid

from local_client import (
    ClientError, ConfigChangedError, MAX_FILES_BYTES, complete_submission, get_assignment_config,
    request_feedback, stable_request_id,
)


MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_SUMMARY_CHARS = 3800
RESULTS_SOURCE = 'results/results.json'


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def row_status(row):
    status = row.get('status')
    if status:
        return {'passed': 'pass', 'failed': 'fail'}.get(status, 'unknown')
    score, maximum = row.get('score'), row.get('max_score')
    if number(score) and number(maximum) and maximum > 0:
        return 'pass' if score >= maximum else 'fail'
    return 'unknown'


def visible_rows(results):
    visibility = results.get('visibility', results.get('test_visibility', 'visible'))
    for index, row in enumerate(results.get('tests', [])):
        if (not row.get('extra_data', {}).get('ai_feedback')
                and row.get('visibility', visibility) == 'visible'):
            yield index, row


def read_artifact(root, relative, limit=MAX_ARTIFACT_BYTES):
    path = PurePosixPath(relative)
    if path.is_absolute() or '..' in path.parts or '\\' in relative:
        raise ClientError("Artifact paths must stay inside the configured root.")
    candidate = root / relative
    candidate.resolve().relative_to(root.resolve())
    if not candidate.is_file() or candidate.stat().st_size > limit:
        raise ClientError("An expected artifact is missing or exceeds its size limit.")
    return candidate.read_text(encoding="utf-8")


def collect_files(submission, analysis):
    primary = []
    context = []
    for patterns, destination in ((analysis['source_globs'], primary),
                                  (analysis.get('context_globs', []), context)):
        for pattern in patterns:
            if PurePosixPath(pattern).is_absolute() or '..' in PurePosixPath(pattern).parts:
                raise ClientError("Source patterns must be submission-relative.")
            destination.extend(sorted(path for path in submission.glob(pattern) if path.is_file()))
    primary = list(dict.fromkeys(primary))
    if not primary:
        raise ClientError("No submitted primary source matches this feedback target.")
    maximum = analysis.get('max_source_files')
    if maximum is not None and len(primary) > maximum:
        raise ClientError("Multiple submitted primary sources match this feedback target; the source selection is ambiguous.")
    files = []
    seen = set()
    for path in primary + context:
        relative = path.relative_to(submission).as_posix()
        if relative not in seen:
            content = read_artifact(submission, relative, MAX_FILES_BYTES)
            files.append({'path': relative, 'content': content})
            seen.add(relative)
    if sum(len(item['content'].encode('utf-8')) for item in files) > MAX_FILES_BYTES:
        raise ClientError("Selected source files exceed the client input limit.")
    return files


def bound_evidence(evidence):
    if len(evidence) > 256:
        raise ClientError('The selected functional results exceed the evidence limit.')
    for item in evidence:
        item['summary'] = item['summary'].strip() or 'No diagnostic text was recorded for this observation.'
    if sum(len(item['summary']) for item in evidence) > 16_000:
        # Retain every selected identity and outcome while sharing the core's text budget.
        per_row_limit = 16_000 // len(evidence)
        marker = '\n[Evidence excerpt truncated.]'
        for item in evidence:
            summary = item['summary']
            if len(summary) > per_row_limit:
                summary = summary[:per_row_limit - len(marker)] + marker
            item['summary'] = summary
    return evidence


def result_evidence(results, source, *, failed_only=False, blocked=()):
    """Keep request observations stable and snapshot-bound row identities local."""
    snapshot = fingerprint({'path': source['path'], 'label': source['label'], 'results': results})
    evidence, mapping = [], {}
    for index, row in visible_rows(results):
        if index in blocked:
            continue
        status = row_status(row)
        if failed_only and status != 'fail':
            continue
        name = str(row.get('name', 'Unnamed functional result'))
        if source.get('include') and not any(re.search(pattern, name + '\n') for pattern in source['include']):
            continue
        observation = {'path': source['path'], 'source': source['label'], 'test_id': name,
                       'score': row.get('score'), 'max_score': row.get('max_score'),
                       'status': status, 'output': row.get('output', '')}
        identifier = f'observation-{fingerprint(observation)}:{len(evidence)}'
        summary = f"Reported score: {row.get('score')}/{row.get('max_score')}.\n{row.get('output', '')}"
        if len(summary) > MAX_SUMMARY_CHARS:
            summary = summary[:MAX_SUMMARY_CHARS] + '\n[Evidence excerpt truncated.]'
        evidence.append({'id': identifier, 'kind': 'runtime', 'source': source['label'],
                         'summary': summary, 'test_id': name, 'scope': '', 'status': status})
        mapping[identifier] = {'index': index, 'result_id': f'result-{snapshot}:{index}',
                               'row_hash': fingerprint(row)}
    return evidence, mapping


def collect_evidence(root, analysis, results=None, blocked=()):
    """Only reports with row-level visibility can enter a student-facing prompt."""
    evidence = []
    for source in analysis.get('evidence_sources', []):
        # Raw JUnit, coverage and text artifacts have no reliable visibility mapping.
        if source['format'] != 'gradescope':
            raise ClientError('Only Gradescope evidence is supported for configured AI behaviors.')
        try:
            if source['path'] == RESULTS_SOURCE and results is not None:
                report, excluded = results, blocked
            else:
                report, _, excluded, _ = clean_results(json.loads(read_artifact(root, source['path'])))
            rows, _ = result_evidence(report, source, blocked=excluded)
            evidence.extend(rows)
        except (OSError, ValueError, KeyError, ClientError):
            raise ClientError('A configured Gradescope report is unavailable; no outcome can be inferred.') from None
    return bound_evidence(evidence)


def functional_evidence(results, analysis, blocked):
    sources = analysis.get('evidence_sources', [])
    if (len(sources) != 1 or sources[0]['format'] != 'gradescope'
            or sources[0]['label'] != analysis.get('functional_result_source')
            or sources[0]['path'] != RESULTS_SOURCE or sources[0].get('include')):
        raise ClientError('Functional feedback requires the complete final Gradescope report.')
    evidence, mapping = result_evidence(results, sources[0], failed_only=True, blocked=blocked)
    return bound_evidence(evidence), mapping


def appended_output(original, text, format_name, heading='AI feedback'):
    """Treat model text as text even when the original diagnostic uses rich markup."""
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    if format_name in ('html', 'simple_format'):
        # Gradescope wraps test output in its own pre element, even for rich HTML.
        wrapped = '\n'.join(textwrap.fill(line, width=100, expand_tabs=False,
                                         replace_whitespace=False, drop_whitespace=False,
                                         break_on_hyphens=False) for line in text.split('\n'))
        body = html.escape(wrapped).replace('\n', '<br>')
        addition = '<div><p><strong>' + html.escape(heading) + '</strong></p><p>' + body + '</p></div>'
        return original + '\n' + addition
    if format_name == 'md':
        longest = max((len(match.group()) for match in re.finditer(r'`+', text)), default=0)
        fence = '`' * max(3, longest + 1)
        return original + '\n\n' + heading + '\n\n' + fence + '\n' + text + '\n' + fence
    reset = '\x1b[0m' if format_name == 'ansi' else ''
    return original + reset + '\n\n' + heading + '\n' + text


def restore_owned_output(container, metadata, key):
    """Restore only an output whose complete generated value still matches."""
    record = metadata.get(key)
    if not record:
        return True
    if fingerprint(container.get('output', '')) != record.get('rendered_hash'):
        return False
    if record.get('had_output'):
        container['output'] = record['original_output']
    else:
        container.pop('output', None)
    del metadata[key]
    return True


def clean_results(original):
    results = deepcopy(original)
    previous_ai, functional, blocked, diagnostics = [], [], set(), []
    for row in results.get('tests', []):
        metadata = row.get('extra_data', {})
        if metadata.get('ai_feedback'):
            previous_ai.append(row)
            continue
        had_inline = 'ai_inline' in metadata
        owners = metadata.get('ai_inline', {})
        for target_id in reversed(list(owners)):
            if not restore_owned_output(row, owners, target_id):
                blocked.add(len(functional))
                diagnostics.append({'target_id': target_id, 'error': 'The original test output changed; inline feedback was preserved without reuse.'})
                break
        if had_inline and not owners:
            metadata.pop('ai_inline', None)
        if had_inline and not metadata:
            row.pop('extra_data', None)
        functional.append(row)
    results['tests'] = functional
    for key in ('score', 'max_score'):
        if number(results.get(key)):
            results[key] = float(Decimal(str(results[key])) - sum(
                (Decimal(str(row[key])) for row in previous_ai if number(row.get(key))), Decimal(0)))
    metadata = results.get('extra_data', {})
    had_grading = 'ai_grading' in metadata or 'ai_grading_notice' in metadata
    if not restore_owned_output(results, metadata, 'ai_grading_notice'):
        diagnostics.append({'error': 'The submission output changed; the existing AI notice was preserved.'})
    metadata.pop('ai_grading', None)
    if had_grading and not metadata:
        results.pop('extra_data', None)
    return results, previous_ai, blocked, diagnostics


def grading_rows(target_id, target, revision, response=None, error=None):
    configured = target['analysis'].get('grading_items', [])
    if not configured:
        raise ClientError('AI grading requires configured scoring items.')
    returned = response.get('items', []) if response is not None else []
    by_id = {item['test_id']: item for item in returned}
    if response is not None and (len(by_id) != len(returned) or set(by_id) != {item['id'] for item in configured}):
        raise ClientError('Backend scoring items do not match the configured criteria.')
    rows = []
    for index, criterion in enumerate(configured, 1):
        item = by_id.get(criterion['id'])
        score, maximum = None, criterion['points']
        if not number(maximum) or maximum <= 0:
            raise ClientError('A scoring item has an invalid maximum.')
        if item is not None:
            verdict = item['verdict']
            score = maximum if verdict == 'pass' else 0 if verdict == 'fail' else None
            if item.get('max_score') != maximum or item.get('suggested_score') != score:
                raise ClientError('Backend scores do not match the configured scoring rules.')
            feedback = item['feedback']
        else:
            feedback = error or 'AI grading did not return a usable result.'
        pending = score is None
        output = ('Pending regrade. The displayed zero is a provisional placeholder, not a failing verdict.'
                  if pending else 'Meets the criterion.' if score == maximum else 'Does not meet the criterion.')
        if feedback:
            output += '\n' + feedback
        metadata = {'ai_feedback': True, 'target_id': target_id, 'criterion_id': criterion['id'],
                    'revision': revision, 'grading_status': 'pending_regrade' if pending else 'complete',
                    'actual_score': score}
        if response is not None:
            metadata['request_id'] = response['request_id']
        label = criterion.get('label', f'Criterion {index}')
        rows.append({'name': f"AI grading: {target['identifier']} / {label}",
                     'score': 0 if pending else score, 'max_score': maximum,
                     'visibility': 'visible', 'output_format': 'text', 'output': output,
                     'extra_data': metadata})
    return rows


def apply_inline(results, target_id, revision, response, mapping):
    items = response.get('failure_feedback', [])
    ids = [item['evidence_id'] for item in items]
    if len(ids) != len(set(ids)) or set(ids) != set(mapping):
        raise ClientError('Backend explanations do not match the selected failed tests.')
    for item in items:
        if not isinstance(item.get('feedback'), str) or not item['feedback'].strip():
            raise ClientError('Backend returned an empty functional-test explanation.')
    updates = []
    for item in items:
        location = mapping[item['evidence_id']]
        row = results['tests'][location['index']]
        if fingerprint(row) != location['row_hash']:
            raise ClientError('A selected functional row changed; no inline feedback was applied.')
        original = row.get('output', '')
        rendered = appended_output(original, item['feedback'], row.get('output_format', results.get('test_output_format', 'text')))
        metadata = {
            'had_output': 'output' in row, 'original_output': original,
            'rendered_hash': fingerprint(rendered), 'evidence_id': item['evidence_id'],
            'result_id': location['result_id'],
            'revision': revision, 'request_id': response['request_id'],
        }
        updates.append((row, rendered, metadata))
    for row, rendered, metadata in updates:
        row.setdefault('extra_data', {}).setdefault('ai_inline', {})[target_id] = metadata
        row['output'] = rendered


def finalize_grading(results, revision, *, unknown=False):
    rows = [row for row in results['tests'] if row.get('extra_data', {}).get('ai_feedback')]
    if not rows and not unknown:
        return
    confirmed = sum((Decimal(str(row['score'])) for row in rows), Decimal(0))
    maximum = sum((Decimal(str(row['max_score'])) for row in rows), Decimal(0))
    pending = sum(row['extra_data'].get('grading_status') == 'pending_regrade' for row in rows)
    functional = Decimal(str(results['score'])) if number(results.get('score')) else sum(
        (Decimal(str(row['score'])) for row in results['tests']
         if not row.get('extra_data', {}).get('ai_feedback') and number(row.get('score'))), Decimal(0))
    provisional = functional + confirmed
    for key, addition in (('score', confirmed), ('max_score', maximum)):
        if number(results.get(key)):
            results[key] = float(Decimal(str(results[key])) + addition)
    incomplete = bool(pending or unknown)
    results.setdefault('extra_data', {})['ai_grading'] = {
        'revision': revision, 'grading_status': 'pending_regrade' if incomplete else 'complete',
        'confirmed_score': float(confirmed), 'max_score': None if unknown else float(maximum),
        'pending_count': pending, 'final_score': None if incomplete else float(confirmed),
        'provisional_total': float(provisional), 'final_total': None if incomplete else float(provisional),
    }
    if incomplete:
        if unknown and not rows:
            message = ('AI configuration unavailable; any configured AI grades are unverified '
                       'and the displayed grade may be provisional.')
        else:
            message = ('AI grading incomplete. Provisional grade only. '
                       f'Confirmed AI points: {confirmed}; '
                       + ('the full AI maximum could not be verified.' if unknown else f'full AI maximum: {maximum}; {pending} item(s) pending regrade.')
                       + ' Pending items display zero temporarily and retain their full maximum. '
                       'The displayed total and exported numeric grade are provisional until regrading completes.')
        metadata = results['extra_data']
        if 'ai_grading_notice' not in metadata:
            original = results.get('output', '')
            rendered = appended_output(original, message, results.get('output_format', 'simple_format'), 'AI grading status')
            metadata['ai_grading_notice'] = {'had_output': 'output' in results, 'original_output': original,
                                            'rendered_hash': fingerprint(rendered)}
            results['output'] = rendered


def read_submission_context(root):
    """Read the current Gradescope run identity, if this is a real autograder run."""
    path = root / 'submission_metadata.json'
    if not path.is_file():
        return None
    try:
        metadata = json.loads(path.read_text(encoding='utf-8'))
        identifier = metadata.get('id') if isinstance(metadata, dict) else None
        if not (type(identifier) is int and identifier > 0
                or isinstance(identifier, str) and re.fullmatch(r'[0-9]+', identifier)):
            raise ValueError
        platform_submission_id = str(identifier)
        submitted_at = metadata['created_at']
        if not isinstance(submitted_at, str) or not submitted_at.strip():
            raise ValueError
        datetime.fromisoformat(submitted_at.replace('Z', '+00:00'))
        student_identifier = None
        users = metadata.get('users') or []
        if users and isinstance(users[0], dict):
            student_identifier = users[0].get('email') or users[0].get('id')
            if student_identifier is not None:
                student_identifier = str(student_identifier)
        return {
            'platform_submission_id': platform_submission_id,
            'submitted_at': submitted_at,
            'student_identifier': student_identifier,
        }
    except (OSError, UnicodeError, TypeError, KeyError, ValueError, json.JSONDecodeError):
        return None


def print_cost_summary(run_id, requests):
    accounted = [record['accounting'] for record in requests.values() if record['accounting'] is not None]
    priced = [item for item in accounted if item['estimated_cost_usd'] is not None]
    unknown = len(requests) - len(priced)
    total = sum((Decimal(item['estimated_cost_usd']) for item in priced), Decimal(0))
    tokens = {}
    for key in ('input_tokens', 'cached_input_tokens', 'output_tokens'):
        reported = [item['usage'][key] for item in accounted if item['usage'][key] is not None]
        tokens[key] = ('unknown' if requests and not reported else str(sum(reported))
                       + (' (partial)' if len(reported) < len(requests) else ''))
    models = sorted({item['model_name'] for item in accounted})
    bases = sorted({item['pricing']['basis'] for item in priced
                    if item['pricing'] is not None and isinstance(item['pricing'].get('basis'), str)})
    print(f'Instructor AI usage for run {run_id}:')
    if models:
        print('Models: ' + ', '.join(models))
    print(f"Reported input tokens: {tokens['input_tokens']} (cached: {tokens['cached_input_tokens']})")
    print(f"Reported output tokens: {tokens['output_tokens']}")
    if unknown and not priced:
        print('Estimated API cost: unavailable')
    else:
        print(f"Estimated API cost: USD {total:.6f}" + (' (partial)' if unknown else ''))
    print(f'Requests: {len(priced)} priced, {unknown} without a cost estimate')
    if bases:
        print('Pricing basis: ' + '; '.join(bases))
    if unknown:
        print('Cost is incomplete. The Web assignment total can include usage reported after this run ends.')


def add_feedback(root=Path('/autograder'), *, run_id=None):
    root = Path(root)
    if run_id is None:
        run_id = uuid.uuid4().hex
    submission_id = None
    try:
        metadata = json.loads(read_artifact(root, 'submission_metadata.json'))
        identifier = metadata.get('id') if isinstance(metadata, dict) else None
        if (type(identifier) is int and identifier > 0
                or isinstance(identifier, str) and re.fullmatch(r'[0-9]+', identifier)):
            submission_id = str(identifier)
    except (OSError, ValueError, ClientError):
        pass
    run_context = {'submission_id': submission_id, 'run_id': run_id}
    token_file = root / 'source/assignment.token'
    submission_context = read_submission_context(root)
    report_deadline = time.monotonic() + 300
    # Leave time to report an exhausted AI budget within the hook deadline.
    deadline = report_deadline - 5
    config = None
    ai_status = 'FAILED'
    completion_reason = 'processed'
    ai_needed = False
    deferred_response = None
    hidden_stdout = False
    accounting_requests = {}
    try:
        config = json.loads((root / 'source/config.json').read_text(encoding='utf-8'))
        results_path = root / RESULTS_SOURCE
        raw_results = results_path.read_bytes()
        original = json.loads(raw_results)
        hidden_stdout = original.get('stdout_visibility', 'hidden') == 'hidden'
        if hidden_stdout:
            print(f"Instructor AI run: {json.dumps(run_context)}")
        base, previous_ai, blocked, cleanup_diagnostics = clean_results(original)
        ai_failed = bool(cleanup_diagnostics)
        manifest = None
        records = []
        results = deepcopy(base)
        unknown_grading = False
        for attempt in range(2):
            results = deepcopy(base)
            records = list(cleanup_diagnostics)
            unknown_grading = False
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ClientError('The total AI time budget was exhausted.')
                manifest = get_assignment_config(backend_url=config['backend_url'], token_file=token_file,
                                                 timeout=min(30, remaining))
                ai_needed = any(target['analysis'].get('behavior') == 'ai_grading'
                                for target in manifest['targets'].values())
                feedback_targets = [target for target in manifest['targets'].values()
                                    if target['analysis'].get('behavior') == 'functional_test_feedback']
                if len(feedback_targets) > 1:
                    raise ClientError('Only one functional-test feedback target is supported per assignment.')
                for target_id, target in manifest['targets'].items():
                    analysis = target['analysis']
                    behavior = analysis.get('behavior')
                    if behavior not in ('ai_grading', 'functional_test_feedback'):
                        ai_failed = True
                        records.append({'target_id': target_id, 'error': 'This legacy target must be migrated to an explicit AI behavior.'})
                        unknown_grading = unknown_grading or target.get('max_score') is not None
                        continue
                    request_id = None
                    try:
                        if behavior == 'functional_test_feedback':
                            evidence, mapping = functional_evidence(base, analysis, blocked)
                            if not evidence:
                                continue
                            ai_needed = True
                        else:
                            evidence = collect_evidence(root, analysis, base, blocked)
                        if time.monotonic() >= deadline:
                            raise ClientError('The total AI time budget was exhausted.')
                        files = collect_files(root / 'submission', analysis)
                        request_id = stable_request_id(target_id, files=files, evidence=evidence,
                                                       namespace=f"{manifest['revision']}:{submission_id or ''}:{run_id}")
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ClientError('The total AI time budget was exhausted.')
                        response = request_feedback(backend_url=config['backend_url'], token_file=token_file,
                                                    target_id=target_id, files=files, evidence=evidence,
                                                    request_id=request_id, config_revision=manifest['revision'],
                                                    run_id=run_id,
                                                    platform_submission_id=(submission_context or {}).get('platform_submission_id'),
                                                    submitted_at=(submission_context or {}).get('submitted_at'),
                                                    student_identifier=(submission_context or {}).get('student_identifier'),
                                                    timeout=min(270, remaining))
                        if response.get('status') in {'deferred', 'inactive'}:
                            deferred_response = response
                            records.append({'target_id': target_id, **response})
                            message = ('AI grading is awaiting scheduled authorization.'
                                       if response['status'] == 'deferred' else
                                       'This submission is inactive; AI grading was not run.')
                            rendered = {row.get('extra_data', {}).get('target_id')
                                        for row in results['tests'] if row.get('extra_data', {}).get('ai_feedback')}
                            for pending_id, pending_target in manifest['targets'].items():
                                if (pending_target['analysis'].get('behavior') == 'ai_grading'
                                        and pending_id not in rendered):
                                    results['tests'].extend(grading_rows(
                                        pending_id, pending_target, manifest['revision'], error=message))
                            break
                        accounting_requests[request_id] = {
                            'request_id': request_id, 'target_id': target_id,
                            'accounting': response.get('accounting'),
                        }
                        if behavior == 'ai_grading':
                            results['tests'].extend(grading_rows(target_id, target, manifest['revision'], response))
                        else:
                            apply_inline(results, target_id, manifest['revision'], response, mapping)
                        records.append({'target_id': target_id, **response})
                    except ConfigChangedError:
                        raise
                    except Exception as exc:
                        ai_failed = True
                        detail = str(exc) if isinstance(exc, ClientError) else 'Could not collect or process the configured AI inputs.'
                        record = {'target_id': target_id, 'error': detail}
                        if request_id is not None:
                            record['request_id'] = request_id
                            if isinstance(exc, ClientError) and (exc.accounting is not None or exc.cost_unknown):
                                accounting_requests[request_id] = {
                                    'request_id': request_id, 'target_id': target_id,
                                    'accounting': exc.accounting,
                                }
                            if request_id in accounting_requests:
                                record['accounting'] = accounting_requests[request_id]['accounting']
                        records.append(record)
                        if behavior == 'ai_grading':
                            results['tests'].extend(grading_rows(target_id, target, manifest['revision'], error=detail))
                break
            except ConfigChangedError:
                if attempt == 0:
                    continue
                failure = 'Assignment configuration changed again. No mixed-version AI result was applied.'
            except Exception as exc:
                failure = str(exc) if isinstance(exc, ClientError) else 'Could not load a valid assignment configuration.'
            ai_failed = True
            # A failed refresh cannot publish results staged under an older revision.
            results = deepcopy(base)
            records = list(cleanup_diagnostics) + [{'error': failure}]
            unknown_grading = (manifest is None
                               or any(number(row.get('max_score')) and row['max_score'] > 0 for row in previous_ai)
                               or any(target['analysis'].get('behavior') == 'ai_grading' or target.get('max_score') is not None
                                      for target in manifest['targets'].values()))
            if manifest is not None:
                for target_id, target in manifest['targets'].items():
                    if target['analysis'].get('behavior') == 'ai_grading':
                        results['tests'].extend(grading_rows(target_id, target, manifest['revision'], error=failure))
                    elif target.get('max_score') is not None:
                        unknown_grading = True
            else:
                for old in previous_ai:
                    if number(old.get('max_score')) and old['max_score'] > 0:
                        row = deepcopy(old)
                        row['score'] = 0
                        row['output'] = 'Pending regrade. The displayed zero is provisional. ' + failure
                        row['extra_data'].update(grading_status='pending_regrade', actual_score=None)
                        results['tests'].append(row)
            break
        recorded_ids = {record.get('request_id') for record in records}
        records.extend({**record, 'accounting_only': True}
                       for request_id, record in accounting_requests.items() if request_id not in recorded_ids)
        for record in records:
            record.update(run_context)
        evidence_path = root / 'results/ai_evidence.json'
        evidence_path.write_text(json.dumps(records, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        evidence_path.chmod(0o600)
        finalize_grading(results, manifest['revision'] if manifest is not None else None,
                         unknown=unknown_grading)
        # Never apply an explanation or score to a different functional-result snapshot.
        if results_path.read_bytes() != raw_results:
            raise ClientError('Functional results changed during AI processing; no AI result was applied.')
        temporary = results_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(results, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        temporary.replace(results_path)
        # A valid insufficient-evidence verdict is still a successful AI request.
        ai_status = 'FAILED' if ai_failed else None if deferred_response else 'SUCCEEDED'
        completion_reason = ('not_needed' if ai_status == 'SUCCEEDED' and not ai_needed
                             and not accounting_requests else 'processed')
        if results.get('stdout_visibility', 'hidden') == 'hidden':
            for record in records:
                target = record.get('target_id', 'assignment')
                if record.get('request_id'):
                    print(f"Instructor AI request [{target}]: {record['request_id']}")
                if record.get('error'):
                    print(f"Instructor AI diagnostic [{target}]: {record['error']}")
                if record.get('status') in {'deferred', 'inactive'}:
                    print(f"Instructor AI status [{target}]: {record['reason']}")
                for item in (*record.get('items', []), *record.get('failure_feedback', [])):
                    identifier = item.get('test_id', item.get('evidence_id', ''))
                    print(f"Instructor AI evidence [{target} / {identifier}]: {item['evidence']}")
    finally:
        if hidden_stdout:
            print_cost_summary(run_id, accounting_requests)
        if ai_status is not None and submission_context and isinstance(config, dict) and config.get('backend_url'):
            remaining = report_deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise ClientError('The AI outcome reporting time budget was exhausted.')
                complete_submission(backend_url=config['backend_url'], token_file=token_file,
                                    ai_status=ai_status, completion_reason=completion_reason,
                                    timeout=min(30, remaining), **submission_context)
            except ClientError as exc:
                print(f'AI outcome was not recorded in the grading database: {exc}', file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/autograder'))
    parser.add_argument('--run-id', help='Override the automatic per-run UUID for intentional replay of the same submission and inputs')
    args = parser.parse_args()
    try:
        add_feedback(args.root, run_id=args.run_id)
    except Exception:
        print('AI feedback unavailable: the original functional result was preserved.', file=sys.stderr)


if __name__ == '__main__':
    main()
