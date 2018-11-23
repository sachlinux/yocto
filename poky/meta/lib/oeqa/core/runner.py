# Copyright (C) 2016 Intel Corporation
# Released under the MIT license (see COPYING.MIT)

import os
import time
import unittest
import logging
import re
import json

from unittest import TextTestResult as _TestResult
from unittest import TextTestRunner as _TestRunner

class OEStreamLogger(object):
    def __init__(self, logger):
        self.logger = logger
        self.buffer = ""

    def write(self, msg):
        if len(msg) > 1 and msg[0] != '\n':
            if '...' in msg:
                self.buffer += msg
            elif self.buffer:
                self.buffer += msg
                self.logger.log(logging.INFO, self.buffer)
                self.buffer = ""
            else:
                self.logger.log(logging.INFO, msg)

    def flush(self):
        for handler in self.logger.handlers:
            handler.flush()

class OETestResult(_TestResult):
    def __init__(self, tc, *args, **kwargs):
        super(OETestResult, self).__init__(*args, **kwargs)

        self.successes = []
        self.starttime = {}
        self.endtime = {}
        self.progressinfo = {}

        # Inject into tc so that TestDepends decorator can see results
        tc.results = self

        self.tc = tc

    def startTest(self, test):
        # May have been set by concurrencytest
        if test.id() not in self.starttime:
            self.starttime[test.id()] = time.time()
        super(OETestResult, self).startTest(test)

    def stopTest(self, test):
        self.endtime[test.id()] = time.time()
        super(OETestResult, self).stopTest(test)
        if test.id() in self.progressinfo:
            self.tc.logger.info(self.progressinfo[test.id()])

        # Print the errors/failures early to aid/speed debugging, its a pain
        # to wait until selftest finishes to see them.
        for t in ['failures', 'errors', 'skipped', 'expectedFailures']:
            for (scase, msg) in getattr(self, t):
                if test.id() == scase.id():
                    self.tc.logger.info(str(msg))
                    break

    def logSummary(self, component, context_msg=''):
        elapsed_time = self.tc._run_end_time - self.tc._run_start_time
        self.tc.logger.info("SUMMARY:")
        self.tc.logger.info("%s (%s) - Ran %d test%s in %.3fs" % (component,
            context_msg, self.testsRun, self.testsRun != 1 and "s" or "",
            elapsed_time))

        if self.wasSuccessful():
            msg = "%s - OK - All required tests passed" % component
        else:
            msg = "%s - FAIL - Required tests failed" % component
        skipped = len(self.skipped)
        if skipped:
            msg += " (skipped=%d)" % skipped
        self.tc.logger.info(msg)

    def _getTestResultDetails(self, case):
        result_types = {'failures': 'FAILED', 'errors': 'ERROR', 'skipped': 'SKIPPED',
                        'expectedFailures': 'EXPECTEDFAIL', 'successes': 'PASSED'}

        for rtype in result_types:
            found = False
            for (scase, msg) in getattr(self, rtype):
                if case.id() == scase.id():
                    found = True
                    break
                scase_str = str(scase.id())

                # When fails at module or class level the class name is passed as string
                # so figure out to see if match
                m = re.search("^setUpModule \((?P<module_name>.*)\)$", scase_str)
                if m:
                    if case.__class__.__module__ == m.group('module_name'):
                        found = True
                        break

                m = re.search("^setUpClass \((?P<class_name>.*)\)$", scase_str)
                if m:
                    class_name = "%s.%s" % (case.__class__.__module__,
                                            case.__class__.__name__)

                    if class_name == m.group('class_name'):
                        found = True
                        break

            if found:
                return result_types[rtype], msg

        return 'UNKNOWN', None

    def addSuccess(self, test):
        #Added so we can keep track of successes too
        self.successes.append((test, None))
        super(OETestResult, self).addSuccess(test)

    def logDetails(self, json_file_dir=None, configuration=None, result_id=None):
        self.tc.logger.info("RESULTS:")

        result = {}
        if hasattr(self.tc, "extraresults"):
            result = self.tc.extraresults

        for case_name in self.tc._registry['cases']:
            case = self.tc._registry['cases'][case_name]

            (status, log) = self._getTestResultDetails(case)

            oeid = -1
            if hasattr(case, 'decorators'):
                for d in case.decorators:
                    if hasattr(d, 'oeid'):
                        oeid = d.oeid

            t = ""
            if case.id() in self.starttime and case.id() in self.endtime:
                t = " (" + "{0:.2f}".format(self.endtime[case.id()] - self.starttime[case.id()]) + "s)"

            self.tc.logger.info("RESULTS - %s - Testcase %s: %s%s" % (case.id(), oeid, status, t))
            if log:
                result[case.id()] = {'status': status, 'log': log}
            else:
                result[case.id()] = {'status': status}

        if json_file_dir:
            tresultjsonhelper = OETestResultJSONHelper()
            tresultjsonhelper.dump_testresult_file(json_file_dir, configuration, result_id, result)

    def wasSuccessful(self):
        # Override as we unexpected successes aren't failures for us
        return (len(self.failures) == len(self.errors) == 0)

class OEListTestsResult(object):
    def wasSuccessful(self):
        return True

class OETestRunner(_TestRunner):
    streamLoggerClass = OEStreamLogger

    def __init__(self, tc, *args, **kwargs):
        kwargs['stream'] = self.streamLoggerClass(tc.logger)
        super(OETestRunner, self).__init__(*args, **kwargs)
        self.tc = tc
        self.resultclass = OETestResult

    def _makeResult(self):
        return self.resultclass(self.tc, self.stream, self.descriptions,
                self.verbosity)

    def _walk_suite(self, suite, func):
        for obj in suite:
            if isinstance(obj, unittest.suite.TestSuite):
                if len(obj._tests):
                    self._walk_suite(obj, func)
            elif isinstance(obj, unittest.case.TestCase):
                func(self.tc.logger, obj)
                self._walked_cases = self._walked_cases + 1

    def _list_tests_name(self, suite):
        from oeqa.core.decorator.oeid import OETestID
        from oeqa.core.decorator.oetag import OETestTag

        self._walked_cases = 0

        def _list_cases_without_id(logger, case):

            found_id = False
            if hasattr(case, 'decorators'):
                for d in case.decorators:
                    if isinstance(d, OETestID):
                        found_id = True

            if not found_id:
                logger.info('oeid missing for %s' % case.id())

        def _list_cases(logger, case):
            oeid = None
            oetag = None

            if hasattr(case, 'decorators'):
                for d in case.decorators:
                    if isinstance(d, OETestID):
                        oeid = d.oeid
                    elif isinstance(d, OETestTag):
                        oetag = d.oetag

            logger.info("%s\t%s\t\t%s" % (oeid, oetag, case.id()))

        self.tc.logger.info("Listing test cases that don't have oeid ...")
        self._walk_suite(suite, _list_cases_without_id)
        self.tc.logger.info("-" * 80)

        self.tc.logger.info("Listing all available tests:")
        self._walked_cases = 0
        self.tc.logger.info("id\ttag\t\ttest")
        self.tc.logger.info("-" * 80)
        self._walk_suite(suite, _list_cases)
        self.tc.logger.info("-" * 80)
        self.tc.logger.info("Total found:\t%s" % self._walked_cases)

    def _list_tests_class(self, suite):
        self._walked_cases = 0

        curr = {}
        def _list_classes(logger, case):
            if not 'module' in curr or curr['module'] != case.__module__:
                curr['module'] = case.__module__
                logger.info(curr['module'])

            if not 'class' in curr  or curr['class'] != \
                    case.__class__.__name__:
                curr['class'] = case.__class__.__name__
                logger.info(" -- %s" % curr['class'])

            logger.info(" -- -- %s" % case._testMethodName)

        self.tc.logger.info("Listing all available test classes:")
        self._walk_suite(suite, _list_classes)

    def _list_tests_module(self, suite):
        self._walked_cases = 0

        listed = []
        def _list_modules(logger, case):
            if not case.__module__ in listed:
                if case.__module__.startswith('_'):
                    logger.info("%s (hidden)" % case.__module__)
                else:
                    logger.info(case.__module__)
                listed.append(case.__module__)

        self.tc.logger.info("Listing all available test modules:")
        self._walk_suite(suite, _list_modules)

    def list_tests(self, suite, display_type):
        if display_type == 'name':
            self._list_tests_name(suite)
        elif display_type == 'class':
            self._list_tests_class(suite)
        elif display_type == 'module':
            self._list_tests_module(suite)

        return OEListTestsResult()

class OETestResultJSONHelper(object):

    testresult_filename = 'testresults.json'

    def _get_existing_testresults_if_available(self, write_dir):
        testresults = {}
        file = os.path.join(write_dir, self.testresult_filename)
        if os.path.exists(file):
            with open(file, "r") as f:
                testresults = json.load(f)
        return testresults

    def _write_file(self, write_dir, file_name, file_content):
        file_path = os.path.join(write_dir, file_name)
        with open(file_path, 'w') as the_file:
            the_file.write(file_content)

    def dump_testresult_file(self, write_dir, configuration, result_id, test_result):
        bb.utils.mkdirhier(write_dir)
        lf = bb.utils.lockfile(os.path.join(write_dir, 'jsontestresult.lock'))
        test_results = self._get_existing_testresults_if_available(write_dir)
        test_results[result_id] = {'configuration': configuration, 'result': test_result}
        json_testresults = json.dumps(test_results, sort_keys=True, indent=4)
        self._write_file(write_dir, self.testresult_filename, json_testresults)
        bb.utils.unlockfile(lf)
