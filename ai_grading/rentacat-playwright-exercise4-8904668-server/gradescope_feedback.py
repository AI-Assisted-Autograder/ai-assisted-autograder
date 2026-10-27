"""Collect configured evidence and append backend feedback to Gradescope results.

This module and local_client.py use only the standard library. Assignment-specific
file and report selection comes from the backend configuration exported at build time.
"""

import argparse
import csv
import io
import json
from pathlib import Path, PurePosixPath
import re
import sys
import time
import xml.etree.ElementTree as ET

from local_client import ClientError, MAX_FILES_BYTES, request_feedback, stable_request_id


MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_SUMMARY_CHARS = 3800


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


def report_rows(text, format_name):
    """Yield (kind, test_id, scope, status, summary) without inferring test coverage."""
    if format_name == 'gradescope':
        results = json.loads(text)
        functional_rows = [row for row in results.get('tests', [])
                           if not row.get('extra_data', {}).get('ai_feedback')]
        for row in functional_rows:
            if row.get('visibility', results.get('test_visibility', 'visible')) != 'visible':
                continue
            name = str(row.get('name', 'Unnamed functional result'))
            score, maximum = row.get('score'), row.get('max_score')
            status = 'unknown'
            if isinstance(score, (int, float)) and isinstance(maximum, (int, float)) and maximum > 0:
                status = 'pass' if score >= maximum else 'fail'
            yield ('runtime', name, '', status,
                   f"Reported score: {score}/{maximum}.\n{row.get('output', '')}")
        if not functional_rows and results.get('output'):
            yield ('runtime', 'execution', '', 'unknown', str(results['output']))
    elif format_name == 'junit':
        tree = ET.fromstring(text)
        for case in tree.iter('testcase'):
            status = 'pass'
            messages = []
            for tag in ('failure', 'error', 'skipped'):
                for result in case.findall(tag):
                    status = {'failure': 'fail', 'error': 'error', 'skipped': 'skipped'}[tag]
                    messages.append((result.get('message', '') + '\n' + (result.text or '')).strip())
            messages.extend(node.text or '' for node in case.findall('system-out'))
            yield ('runtime', case.get('name', 'unnamed'), case.get('classname', ''), status,
                   '\n'.join(messages) or f"JUnit reported {status}.")
    elif format_name == 'jacoco':
        if text.lstrip().startswith('<'):
            tree = ET.fromstring(text)
            for cls in tree.iter('class'):
                counts = []
                for counter in cls.findall('counter'):
                    covered, missed = int(counter.attrib['covered']), int(counter.attrib['missed'])
                    counts.append(f"{counter.attrib['type']}: {covered} covered / {covered + missed} total")
                if counts:
                    yield ('coverage', None, cls.attrib['name'], 'unknown',
                           'Aggregate class coverage from this run; not attributed to individual tests.\n'
                           + '\n'.join(counts))
        else:
            for row in csv.DictReader(io.StringIO(text)):
                counts = []
                for kind in ('INSTRUCTION', 'BRANCH', 'LINE', 'METHOD', 'COMPLEXITY'):
                    if kind + '_COVERED' in row:
                        covered, missed = int(row[kind + '_COVERED']), int(row[kind + '_MISSED'])
                        counts.append(f'{kind}: {covered} covered / {covered + missed} total')
                if counts:
                    scope = '/'.join(filter(None, (row.get('PACKAGE', '').replace('.', '/'), row['CLASS'])))
                    yield ('coverage', None, scope, 'unknown',
                           'Aggregate class coverage from this run; not attributed to individual tests.\n'
                           + '\n'.join(counts))
    elif format_name == 'text':
        if text.strip():
            yield ('diagnostic', None, '', 'unknown', text)
    else:
        raise ClientError('Unsupported evidence format in assignment configuration.')


def collect_evidence(root, analysis):
    evidence = []
    for source in analysis.get('evidence_sources', []):
        label = source['label']
        try:
            raw = read_artifact(root, source['path'])
            rows = list(report_rows(raw, source['format']))
        except (OSError, ValueError, KeyError, ET.ParseError, ClientError):
            evidence.append({'id': label + ':unavailable', 'kind': 'diagnostic', 'source': label,
                             'summary': 'The configured artifact is missing, unreadable or invalid; no execution outcome can be inferred.',
                             'scope': label, 'status': 'unknown'})
            continue
        selected = 0
        for index, (kind, test_id, scope, status, summary) in enumerate(rows):
            if source['format'] == 'text':
                scope = label
            selection = (test_id or '') + '\n' + scope
            if source.get('include') and not any(re.search(pattern, selection) for pattern in source['include']):
                continue
            if len(summary) > MAX_SUMMARY_CHARS:
                summary = summary[:MAX_SUMMARY_CHARS] + '\n[Evidence excerpt truncated.]'
            evidence.append({'id': f'{label}:{index}', 'kind': kind, 'source': label,
                             'summary': summary, 'test_id': test_id, 'scope': scope, 'status': status})
            selected += 1
        if not selected:
            evidence.append({'id': label + ':empty', 'kind': 'diagnostic', 'source': label,
                             'summary': 'No matching observations were present in the configured artifact; no pass or failure is inferred.',
                             'scope': label, 'status': 'unknown'})
    if len(evidence) > 256:
        omitted = evidence[255:]
        source = omitted[0]['source']
        evidence = evidence[:255] + [{
            'id': 'evidence:overflow', 'kind': 'diagnostic', 'source': source,
            'summary': f'{len(omitted)} observations omitted; their outcomes are unknown.',
            'scope': source, 'status': 'unknown',
        }]
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


def feedback_row(name, output, metadata, *, score=None, max_score=None):
    return {'name': name, 'score': score if score is not None else 0,
            'max_score': max_score if score is not None else 0, 'visibility': 'visible',
            'output_format': 'text', 'output': output,
            'extra_data': {'ai_feedback': True, **metadata}}


def add_feedback(root=Path('/autograder'), *, run_id=None):
    started = time.monotonic()
    root = Path(root)
    results_path = root / 'results/results.json'
    results = json.loads(results_path.read_text(encoding='utf-8'))
    config = json.loads((root / 'source/config.json').read_text(encoding='utf-8'))
    manifest = json.loads((root / 'source/ai_feedback.json').read_text(encoding='utf-8'))
    deadline = started + manifest.get('total_timeout', 300)
    # Re-running just the hook replaces its own rows, never functional rows.
    tests = results.setdefault('tests', [])
    previous_ai = [row for row in tests if row.get('extra_data', {}).get('ai_feedback')]
    results['tests'] = [row for row in tests if not row.get('extra_data', {}).get('ai_feedback')]
    instructor_results = []
    for target_id, target in manifest['targets'].items():
        name = f"AI Feedback: {target['identifier']}"
        try:
            if time.monotonic() >= deadline:
                raise ClientError('The total AI feedback time budget was exhausted.')
            files = collect_files(root / 'submission', target['analysis'])
            evidence = collect_evidence(root, target['analysis'])
            request_id = stable_request_id(target_id, files=files, evidence=evidence,
                                           namespace=run_id or manifest.get('request_namespace', ''))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ClientError('The total AI feedback time budget was exhausted.')
            response = request_feedback(backend_url=config['backend_url'],
                                        token_file=root / 'source/assignment.token',
                                        target_id=target_id, files=files, evidence=evidence,
                                        request_id=request_id, timeout=min(manifest.get('timeout', 270), remaining))
            metadata = {key: response[key] for key in ('engine', 'provider', 'requested_model',
                                                      'model_name', 'request_id')}
            metadata['target_id'] = target_id
            items = response.get('items', [])
            if target['analysis']['mode'] != 'code_feedback' and not items:
                raise ClientError('Backend returned no structured feedback for this target.')
            if items:
                for item in items:
                    verdict = {'pass': 'Meets the criterion', 'fail': 'Does not meet the criterion',
                               'insufficient_evidence': 'Insufficient evidence'}[item['verdict']]
                    output = verdict + (f"\n{item['feedback']}" if item['feedback'] else '')
                    results['tests'].append(feedback_row(f"{name} / {item['test_id']}", output, metadata,
                                                        score=item.get('suggested_score'), max_score=item.get('max_score')))
            else:
                results['tests'].append(feedback_row(name, response['feedback_text'], metadata))
            if response.get('suggested_score') is not None and not any(item.get('max_score') is not None for item in items):
                results['tests'].append(feedback_row(f"AI Score: {target['identifier']}", response['feedback_text'], metadata,
                                                    score=response['suggested_score'], max_score=response['max_score']))
            instructor_results.append({'target_id': target_id, **response})
        except Exception as exc:
            detail = str(exc) if isinstance(exc, ClientError) else 'Could not collect or process configured feedback inputs.'
            results['tests'].append(feedback_row(name,
                'AI feedback is unavailable. Your functional-test score is unchanged.\n' + detail,
                {'target_id': target_id}))
    # Explicit top-level totals override Gradescope row totals. Adjust only our
    # own contribution, preserving the functional grader's original arithmetic.
    new_ai = [row for row in results['tests'] if row.get('extra_data', {}).get('ai_feedback')]
    for key in ('score', 'max_score'):
        if isinstance(results.get(key), (int, float)):
            results[key] += sum(row[key] for row in new_ai) - sum(row[key] for row in previous_ai)
    # Teacher evidence is retained separately from student-visible rows.
    evidence_path = root / 'results/ai_evidence.json'
    evidence_path.write_text(json.dumps(instructor_results, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    evidence_path.chmod(0o600)
    for result in instructor_results:
        for item in result.get('items', []):
            print(f"Instructor AI evidence [{result['target_id']} / {item['test_id']}]: {item['evidence']}", file=sys.stderr)
    temporary = results_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(results_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/autograder'))
    parser.add_argument('--run-id', help='New namespace for an independent run; reuse it for retries')
    args = parser.parse_args()
    try:
        add_feedback(args.root, run_id=args.run_id)
    except Exception:
        print('AI feedback unavailable: the original functional result was preserved.', file=sys.stderr)


if __name__ == '__main__':
    main()
