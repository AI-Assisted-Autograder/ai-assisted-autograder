import static com.github.tkutche1.jgrade.gradedtest.GradedTestResult.VISIBLE;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.List;
import java.util.stream.Collectors;

import com.github.tkutche1.jgrade.Grader;
import com.github.tkutche1.jgrade.gradedtest.GradedTestResult;

import org.openmbee.junit.JUnitMarshalling;
import org.openmbee.junit.model.JUnitError;
import org.openmbee.junit.model.JUnitFailure;
import org.openmbee.junit.model.JUnitTestCase;
import org.openmbee.junit.model.JUnitTestSuite;

/**
 * Grades the Playwright JUnit report produced by autograder/grade.sh.
 *
 * The report contains two suites: rentacat.spec.ts (the student's tests run
 * against the correct website) and rentacatbuggy.spec.ts (the same tests with
 * every URL pointed at the buggier website).  A test earns its points when it
 * passes on the correct website and fails on the buggier one.
 *
 * Each test case gets its own row on Gradescope.
 */
public class RentACatTestGrading {

	static final String CORRECT_SITE = "https://cs1632.appspot.com/";
	static final String BUGGY_SITE = "https://cs1632-buggier.appspot.com/";
	static final String SPEC = "rentacat.spec.ts";
	static final String BUGGY_SPEC = "rentacatbuggy.spec.ts";

	static final String[] TESTS = { "TEST-1-RESET", "TEST-2-CATALOG", "TEST-3-LISTING", "TEST-4-RENT-A-CAT",
			"TEST-5-RENT", "TEST-6-RETURN", "TEST-7-FEED-A-CAT", "TEST-8-FEED", "TEST-9-GREET-A-CAT",
			"TEST-10-GREET-A-CAT-WITH-NAME", "TEST-11-FEED-A-CAT-SCREENSHOT" };
	static final String SCREENSHOT_TEST = "TEST-11-FEED-A-CAT-SCREENSHOT";

	static final double POINTS_PER_TEST = 5;
	static final double MAX_SCORE = TESTS.length * POINTS_PER_TEST;

	static final Path REPORT_DIR = Paths.get("submission", "playwright-report");
	static final Path REPORT_FILE = REPORT_DIR.resolve("results.xml");
	static final Path BUGGY_REPORT_FILE = REPORT_DIR.resolve("results-buggy.xml");
	static final Path NOTES_FILE = REPORT_DIR.resolve("notes.txt");
	static final Path RUN_LOG = REPORT_DIR.resolve("run.log");
	static final Path BUGGY_RUN_LOG = REPORT_DIR.resolve("run-buggy.log");

	static final int LOG_TAIL_LINES = 40;
	static final int MAX_FAILURE_CHARS = 3000;

	public void grade(Grader grader) {
		// Notes written by grade.sh (missing file, test.only removed, etc.) go at the top.
		String notes = readFile(NOTES_FILE).trim();
		if (!notes.isEmpty()) {
			grader.addOutput(notes + "\n");
		}

		boolean specMissing = notes.contains("Could not find tests/rentacat.spec.ts");
		Report report = Report.read(REPORT_FILE);
		Report buggyReport = Report.read(BUGGY_REPORT_FILE);
		List<JUnitTestCase> cases = findSuite(report.suites, SPEC);
		List<JUnitTestCase> buggyCases = findSuite(buggyReport.suites, BUGGY_SPEC);

		if (cases == null || cases.isEmpty()) {
			GradedTestResult r = new GradedTestResult("Playwright test results", "", MAX_SCORE, VISIBLE);
			r.setScore(0);
			if (specMissing) {
				r.addOutput("No tests were run because rentacat.spec.ts was not found (see the note above).\n");
			} else if (!report.exists) {
				r.addOutput(crashMessage(CORRECT_SITE));
				appendLogTail(r, RUN_LOG);
			} else {
				r.addOutput("Playwright did not report any test results for " + SPEC + ". This usually means "
						+ "the file has no test cases or has a syntax/TypeScript error.\n");
				if (report.parseError != null) {
					r.addOutput("(The Playwright report could not be read: " + report.parseError + ")\n");
				}
				appendLogTail(r, RUN_LOG);
			}
			grader.addGradedTestResult(r);
			return;
		}
		if (buggyCases == null) {
			GradedTestResult r = new GradedTestResult("Playwright test results", "", MAX_SCORE, VISIBLE);
			r.setScore(0);
			if (!buggyReport.exists) {
				r.addOutput(crashMessage(BUGGY_SITE));
			} else {
				// Should not happen: the buggy suite is a copy of the student's suite.
				r.addOutput("The autograder could not find results for the tests run against " + BUGGY_SITE
						+ ". This is probably not your fault; please contact the instructor.\n");
			}
			appendLogTail(r, BUGGY_RUN_LOG);
			grader.addGradedTestResult(r);
			return;
		}

		for (int i = 0; i < TESTS.length; i++) {
			grader.addGradedTestResult(gradeTest(TESTS[i], i + 1, cases, buggyCases));
		}
	}

	private GradedTestResult gradeTest(String id, int number, List<JUnitTestCase> cases,
			List<JUnitTestCase> buggyCases) {
		GradedTestResult r = new GradedTestResult(id, String.valueOf(number), POINTS_PER_TEST, VISIBLE);
		r.setScore(0);

		JUnitTestCase tc = findTest(cases, id);
		JUnitTestCase buggy = findTest(buggyCases, id);

		if (tc == null) {
			r.addOutput("Not implemented: no test named \"" + id + "\" was found in " + SPEC
					+ ". The test title must be exactly the IDENTIFIER from testplan.md.\n");
		} else if (isSkipped(tc)) {
			r.addOutput("Skipped on " + CORRECT_SITE + " (test.skip, test.fixme, or an earlier failure in "
					+ "serial mode), so it could not be graded.\n");
		} else if (isFailed(tc)) {
			r.addOutput("Failed on " + CORRECT_SITE + ", where it should pass:\n\n" + failureText(tc) + "\n");
			if (id.equals(SCREENSHOT_TEST) && mentionsMissingSnapshot(tc)) {
				r.addOutput("\nHint: Playwright could not find the screenshot to compare against. Call "
						+ "toHaveScreenshot() without a custom name, and do not put " + id + " inside a "
						+ "test.describe block, since both change the screenshot file name.\n");
			}
		} else if (buggy == null) {
			r.addOutput("Passed on " + CORRECT_SITE + ", but the autograder could not find this test's "
					+ "result on " + BUGGY_SITE + ". Please contact the instructor.\n");
		} else if (isSkipped(buggy)) {
			r.addOutput("Passed on " + CORRECT_SITE + ", but was skipped on " + BUGGY_SITE + ", so the "
					+ "autograder could not tell whether it catches the defect. This usually happens in "
					+ "serial mode (test.describe.configure({ mode: 'serial' })): once one test fails, "
					+ "Playwright skips the rest. Please make your tests independent.\n");
		} else if (!isFailed(buggy)) {
			r.addOutput("Passed on " + CORRECT_SITE + " but also passed on " + BUGGY_SITE
					+ ", even though that website contains a defect this test should catch. Check your "
					+ "locators and assertions.\n");
		} else {
			r.setScore(POINTS_PER_TEST);
			r.addOutput("Passed on " + CORRECT_SITE + " and failed on " + BUGGY_SITE + ". Good job!\n");
		}
		return r;
	}

	/** One parsed Playwright JUnit report file. */
	private static final class Report {
		boolean exists;
		List<JUnitTestSuite> suites;
		String parseError;

		static Report read(Path path) {
			Report report = new Report();
			report.exists = Files.exists(path);
			if (report.exists) {
				try (InputStream in = Files.newInputStream(path)) {
					report.suites = JUnitMarshalling.unmarshalTestSuite(in).getTestSuites();
				} catch (Exception ex) {
					report.parseError = ex.toString();
				}
			}
			return report;
		}
	}

	/** Playwright writes its JUnit report only at the very end, so a missing report means it never finished. */
	private static String crashMessage(String site) {
		return "Playwright stopped before it finished running your tests on " + site + ", so no results "
				+ "were recorded. This is usually caused by the autograder running out of memory or time, "
				+ "not by your code. Please resubmit; if it happens again, contact the instructor.\n";
	}

	private static List<JUnitTestCase> findSuite(List<JUnitTestSuite> suites, String name) {
		if (suites == null) {
			return null;
		}
		for (JUnitTestSuite suite : suites) {
			if (name.equals(suite.getName())) {
				return suite.getTestCases();
			}
		}
		return null;
	}

	/**
	 * Playwright reports a test inside test.describe('X', ...) as "X > TEST-1-RESET",
	 * where ">" is really the U+203A character,
	 * so match the last segment of the title as well as the whole title.
	 */
	private static JUnitTestCase findTest(List<JUnitTestCase> cases, String id) {
		if (cases == null) {
			return null;
		}
		JUnitTestCase nested = null;
		for (JUnitTestCase tc : cases) {
			String name = tc.getName() == null ? "" : tc.getName().trim();
			if (name.equals(id)) {
				return tc;
			}
			if (nested == null && name.endsWith(" \u203A " + id)) {
				nested = tc;
			}
		}
		return nested;
	}

	private static boolean isSkipped(JUnitTestCase tc) {
		return tc.getSkipped() != null;
	}

	private static boolean isFailed(JUnitTestCase tc) {
		return (tc.getFailures() != null && !tc.getFailures().isEmpty())
				|| (tc.getErrors() != null && !tc.getErrors().isEmpty());
	}

	private static String failureText(JUnitTestCase tc) {
		StringBuilder sb = new StringBuilder();
		if (tc.getFailures() != null) {
			for (JUnitFailure f : tc.getFailures()) {
				sb.append(firstNonBlank(f.getValue(), f.getMessage())).append('\n');
			}
		}
		if (tc.getErrors() != null) {
			for (JUnitError e : tc.getErrors()) {
				sb.append(firstNonBlank(e.getValue(), e.getMessage())).append('\n');
			}
		}
		String text = stripAnsi(sb.toString()).trim();
		if (text.length() > MAX_FAILURE_CHARS) {
			text = text.substring(0, MAX_FAILURE_CHARS) + "\n... (truncated)";
		}
		return text;
	}

	private static boolean mentionsMissingSnapshot(JUnitTestCase tc) {
		String text = failureText(tc).toLowerCase();
		return text.contains("snapshot") && text.contains("doesn't exist");
	}

	private static void appendLogTail(GradedTestResult r, Path logFile) {
		String log = stripAnsi(readFile(logFile)).trim();
		if (log.isEmpty()) {
			return;
		}
		List<String> lines = log.lines().collect(Collectors.toList());
		int from = Math.max(0, lines.size() - LOG_TAIL_LINES);
		r.addOutput("\nLast lines of Playwright's output:\n\n");
		r.addOutput(String.join("\n", lines.subList(from, lines.size())) + "\n");
	}

	private static String firstNonBlank(String a, String b) {
		if (a != null && !a.trim().isEmpty()) {
			return a;
		}
		return b == null ? "" : b;
	}

	private static String stripAnsi(String s) {
		return s.replaceAll("\u001B\\[[;?0-9]*[A-Za-z]", "");
	}

	private static String readFile(Path path) {
		try {
			return new String(Files.readAllBytes(path), StandardCharsets.UTF_8);
		} catch (IOException ex) {
			return "";
		}
	}
}
