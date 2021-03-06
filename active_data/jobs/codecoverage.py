# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#
from __future__ import absolute_import
from __future__ import division
from __future__ import unicode_literals

from mo_dots import coalesce, wrap, unwrap
from pyLibrary.collections import UNION, MIN
from mo_logs import constants
from mo_logs import startup
from mo_logs import Log
from pyLibrary.env import http, elasticsearch
from pyLibrary.queries import jx
from mo_threads import Thread, Signal, Queue
from mo_times.dates import Date, unicode2Date
from mo_times.timer import Timer

DEBUG = True
NUM_THREAD = 4


def process_batch(todo, coverage_index, coverage_summary_index, settings, please_stop):
    for not_summarized in todo:
        if please_stop:
            return True

        # IS THERE MORE THAN ONE COVERAGE FILE FOR THIS REVISION?
        Log.note("Find dups for file {{file}}", file=not_summarized.source.file.name)
        dups = http.post_json(settings.url, json={
            "from": "coverage",
            "select": [
                {"name": "max_id", "value": "etl.source.id", "aggregate": "max"},
                {"name": "min_id", "value": "etl.source.id", "aggregate": "min"}
            ],
            "where": {"and": [
                {"missing": "source.method.name"},
                {"eq": {
                    "source.file.name": not_summarized.source.file.name,
                    "build.revision12": not_summarized.build.revision12
                }},
            ]},
            "groupby": [
                "test.url"
            ],
            "limit": 100000,
            "format": "list"
        })

        dups_found = False
        for d in dups.data:
            if d.max_id != d.min_id:
                # FIND ALL INDEXES
                dups_found = True
                Log.note(
                    "removing dups {{details|json}}",
                    details={"and": [
                        {"not": {"term": {"etl.source.id": int(d.max_id)}}},
                        {"term": {"test.url": d.test.url}},
                        {"term": {"source.file.name": not_summarized.source.file.name}},
                        {"term": {"build.revision12": not_summarized.build.revision12}}
                    ]}
                )

                coverage_index.delete_record({"and": [
                    {"not": {"term": {"etl.source.id": int(d.max_id)}}},
                    {"term": {"test.url": d.test.url}},
                    {"term": {"source.file.name": not_summarized.source.file.name}},
                    {"term": {"build.revision12": not_summarized.build.revision12}}
                ]})
        if dups_found:
            continue

        # LIST ALL TESTS THAT COVER THIS FILE, AND THE LINES COVERED
        test_count = http.post_json(settings.url, json={
            "from": "coverage.source.file.covered",
            "where": {"and": [
                {"missing": "source.method.name"},
                {"eq": {
                    "source.file.name": not_summarized.source.file.name,
                    "build.revision12": not_summarized.build.revision12
                }},
            ]},
            "groupby": [
                "test.url",
                "line"
            ],
            "limit": 100000,
            "format": "list"
        })

        all_tests_covering_file = UNION(test_count.data.get("test.url"))
        num_tests = len(all_tests_covering_file)
        max_siblings = num_tests - 1
        Log.note(
            "{{filename}} rev {{revision}} is covered by {{num}} tests",
            filename=not_summarized.source.file.name,
            num=num_tests,
            revision=not_summarized.build.revision12
        )
        line_summary = list(
            (k, unwrap(wrap(list(v)).get("test.url")))
            for k, v in jx.groupby(test_count.data, keys="line")
        )

        # PULL THE RAW RECORD FOR MODIFICATION
        file_level_coverage_records = http.post_json(settings.url, json={
            "from": "coverage",
            "where": {"and": [
                {"missing": "source.method.name"},
                {"in": {"test.url": all_tests_covering_file}},
                {"eq": {
                    "source.file.name": not_summarized.source.file.name,
                    "build.revision12": not_summarized.build.revision12
                }}
            ]},
            "limit": 100000,
            "format": "list"
        })

        for test_name in all_tests_covering_file:
            siblings = [len(test_names)-1 for g, test_names in line_summary if test_name in test_names]
            min_siblings = MIN(siblings)
            coverage_candidates = jx.filter(file_level_coverage_records.data, lambda row, rownum, rows: row.test.url == test_name)
            if coverage_candidates:

                if len(coverage_candidates) > 1 and any(coverage_candidates[0]._id != c._id for c in coverage_candidates):
                    Log.warning(
                        "Duplicate coverage\n{{cov|json|indent}}",
                        cov=[{"_id": c._id, "run": c.run, "test": c.test} for c in coverage_candidates]
                    )

                # MORE THAN ONE COVERAGE CANDIDATE CAN HAPPEN WHEN THE SAME TEST IS IN TWO DIFFERENT CHUNKS OF THE SAME SUITE
                for coverage_record in coverage_candidates:
                    coverage_record.source.file.max_test_siblings = max_siblings
                    coverage_record.source.file.min_line_siblings = min_siblings
                    coverage_record.source.file.score = (max_siblings - min_siblings) / (max_siblings + min_siblings + 1)
            else:
                example = http.post_json(settings.url, json={
                    "from": "coverage",
                    "where": {"eq": {
                        "test.url": test_name,
                        "source.file.name": not_summarized.source.file.name,
                        "build.revision12": not_summarized.build.revision12
                    }},
                    "limit": 1,
                    "format": "list"
                })

                Log.warning(
                    "{{test|quote}} rev {{revision}} appears to have no coverage for {{file|quote}}!\n{{example|json|indent}}",
                    test=test_name,
                    file=not_summarized.source.file.name,
                    revision=not_summarized.build.revision12,
                    example=example.data[0]
                )

        bad_example = [d for d in file_level_coverage_records.data if d["source.file.min_line_siblings"] == None]
        if bad_example:
            Log.warning("expecting all records to have summary. Example:\n{{example}}", example=bad_example[0])

        rows = [{"id": d._id, "value": d} for d in file_level_coverage_records.data]
        coverage_summary_index.extend(rows)
        coverage_index.extend(rows)

        all_test_summary = []
        for g, records in jx.groupby(file_level_coverage_records.data, "source.file.name"):
            g = unwrap(g)
            cov = UNION(records.source.file.covered)
            uncov = UNION(records.source.file.uncovered)
            coverage = {
                "_id": "|".join([records[0].build.revision12, g["source.file.name"]]),  # SOMETHING UNIQUE, IN CASE WE RECALCULATE
                "source": {
                    "file": {
                        "name": g["source.file.name"],
                        "is_file": True,
                        "covered": jx.sort(cov, "line"),
                        "uncovered": jx.sort(uncov),
                        "total_covered": len(cov),
                        "total_uncovered": len(uncov),
                        "min_line_siblings": 0  # PLACEHOLDER TO INDICATE DONE
                    }
                },
                "build": records[0].build,
                "repo": records[0].repo,
                "run": records[0].run,
                "etl": {"timestamp": Date.now()}
            }
            all_test_summary.append(coverage)

        sum_rows = [{"id": d["_id"], "value": d} for d in all_test_summary]
        coverage_summary_index.extend(sum_rows)

        if DEBUG:
            coverage_index.refresh()
            todo = http.post_json(settings.url, json={
                "from": "coverage",
                "where": {"and": [
                    {"missing": "source.method.name"},
                    {"missing": "source.file.min_line_siblings"},
                    {"eq": {"source.file.name": not_summarized.source.file.name}},
                    {"eq": {"build.revision12": not_summarized.build.revision12}}
                ]},
                "format": "list",
                "limit": 10
            })
            if todo.data:
                Log.error("Failure to update")


def loop(source, coverage_summary_index, settings, please_stop):
    Log.note("Started loop")
    try:
        cluster = elasticsearch.Cluster(source)
        aliases = cluster.get_aliases()
        candidates = []
        for pairs in aliases:
            if pairs.alias == source.index:
                candidates.append(pairs.index)
        candidates = jx.sort(candidates, {".": "desc"})

        for index_name in candidates:
            coverage_index = elasticsearch.Index(index=index_name, read_only=False, settings=source)
            push_date_filter = unicode2Date(coverage_index.settings.index[-15::], elasticsearch.INDEX_DATE_FORMAT)

            while not please_stop:
                # IDENTIFY NEW WORK
                with Timer("Pulling work from index {{index}}", param={"index":index_name}):
                    coverage_index.refresh()

                    todo = http.post_json(settings.url, json={
                        "from": "coverage",
                        "groupby": ["source.file.name", "build.revision12"],
                        "where": {"and": [
                            {"missing": "source.method.name"},
                            {"missing": "source.file.min_line_siblings"},
                            {"gte": {"repo.push.date": push_date_filter}}
                        ]},
                        "format": "list",
                        "limit": coalesce(settings.batch_size, 100)
                    })

                if not todo.data:
                    break

                queue = Queue("pending source files to review")
                queue.extend(todo.data[0:coalesce(settings.batch_size, 100):])

                num_threads = coalesce(settings.threads, NUM_THREAD)
                Log.note("Launch {{num}} threads", num=num_threads)
                threads = [
                    Thread.run(
                        "processor" + unicode(i),
                        process_batch,
                        queue,
                        coverage_index,
                        coverage_summary_index,
                        settings,
                        please_stop=please_stop
                    )
                    for i in range(num_threads)
                ]

                # ADD STOP MESSAGE
                queue.add(Thread.STOP)

                # WAIT FOR THEM TO COMPLETE
                for t in threads:
                    t.join()
        return

    except Exception, e:
        Log.warning("Problem processing", cause=e)
    finally:
        Log.note("Processing loop is done")
        please_stop.go()


def main():
    try:
        config = startup.read_settings()
        with startup.SingleInstance(flavor_id=config.args.filename):
            constants.set(config.constants)
            Log.start(config.debug)

            please_stop = Signal("main stop signal")
            coverage_index = elasticsearch.Cluster(config.source).get_index(settings=config.source)
            config.destination.schema = coverage_index.get_schema()
            coverage_summary_index = elasticsearch.Cluster(config.destination).get_or_create_index(read_only=False, settings=config.destination)
            coverage_summary_index.add_alias(config.destination.index)
            Log.note("start processing")
            Thread.run(
                "processing loop",
                loop,
                config.source,
                coverage_summary_index,
                config,
                please_stop=please_stop
            )
            Thread.wait_for_shutdown_signal(please_stop)
    except Exception, e:
        Log.error("Problem with code coverage score calculation", cause=e)
    finally:
        Log.stop()


if __name__ == "__main__":
    main()

