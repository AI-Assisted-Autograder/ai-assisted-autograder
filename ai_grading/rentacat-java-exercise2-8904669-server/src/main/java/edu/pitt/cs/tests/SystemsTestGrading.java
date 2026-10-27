package edu.pitt.cs.tests;

import edu.pitt.cs.*;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.PrintStream;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import org.junit.Ignore;
import org.junit.Test;
import org.junit.runner.JUnitCore;
import org.junit.runner.Request;
import org.junit.runner.Result;
import org.junit.runner.notification.Failure;

import com.github.tkutche1.jgrade.gradedtest.GradedTestResult;
import static com.github.tkutche1.jgrade.gradedtest.GradedTestResult.VISIBLE;

/**
 * Grades the systems tests the student wrote in SystemsTest.java.
 *
 * A systems test is only meaningful if it distinguishes a working application
 * from a broken one, so each test method is run twice: once against the
 * student's own RentACatImpl, where it must pass, and once against
 * RentACatNull, whose main method returns immediately without printing
 * anything, where it must fail. A test that passes both times is not asserting
 * anything about the application's behavior and earns no credit.
 *
 * SystemsTest.java is referenced by name rather than by type so that a
 * submission without the file still compiles, and its absence can be reported
 * as a zero with an explanation instead of a compiler error.
 */
public class SystemsTestGrading {

	private static final String TEST_CLASS = "edu.pitt.cs.SystemsTest";
	private static final String IMPL_CLASS = "edu.pitt.cs.RentACatImpl";
	private static final String NULL_RESOURCE = "/edu/pitt/cs/RentACatNull.class";
	private static final String TEST_RESOURCE = "/edu/pitt/cs/SystemsTest.class";

	private static final double MAX_POINTS = 10;
	private static final double DEDUCTION = 1;

	/** Per test method, per implementation. Generous: main is a REPL loop. */
	private static final long TIMEOUT_MS = 15000;

	private double points = MAX_POINTS;

	private GradedTestResult initResult() {
		return new GradedTestResult("SystemsTest on RentACatImpl and RentACatNull", "", MAX_POINTS, VISIBLE);
	}

	/**
	 * The outcome of running one test method once.
	 */
	private static class Outcome {
		static final int PASSED = 0;
		static final int FAILED = 1;
		static final int TIMED_OUT = 2;
		static final int NOT_RUN = 3;

		final int status;
		final String detail;

		Outcome(int status, String detail) {
			this.status = status;
			this.detail = detail;
		}
	}

	public GradedTestResult test() {
		GradedTestResult result = initResult();
		result.addOutput("This section is worth " + (int) MAX_POINTS + " points.  Each JUnit test in"
				+ " SystemsTest.java has to do two things to earn credit: pass when run against your own"
				+ " RentACatImpl, and fail when run against RentACatNull, a stub whose main method returns"
				+ " immediately without printing anything.  A test that passes against both is not checking"
				+ " the behavior of your application, usually because it is missing assertions.  Each test"
				+ " that does not do both loses " + (int) DEDUCTION + " point.\n\n");

		// Locate the student's test class.
		Class<?> studentTest;
		try {
			studentTest = Class.forName(TEST_CLASS);
		} catch (Throwable t) {
			result.setScore(0);
			result.addOutput("(points = 0) No SystemsTest class was found.  Create SystemsTest.java under"
					+ " src/test/java/edu/pitt/cs alongside the other JUnit test classes, and add JUnit tests"
					+ " to it that test the main method.\n\n");
			return result;
		}

		// Collect the JUnit test methods, in a stable order. Declared methods
		// are included as well as public ones, so that a test method the
		// student forgot to declare public is reported as such rather than
		// silently overlooked.
		Map<String, Method> byName = new TreeMap<String, Method>();
		for (Method m : studentTest.getMethods()) {
			if (m.isAnnotationPresent(Test.class)) {
				byName.put(m.getName(), m);
			}
		}
		for (Method m : studentTest.getDeclaredMethods()) {
			if (m.isAnnotationPresent(Test.class) && !byName.containsKey(m.getName())) {
				byName.put(m.getName(), m);
			}
		}
		List<Method> annotated = new ArrayList<Method>(byName.values());

		if (annotated.isEmpty()) {
			result.setScore(0);
			result.addOutput("(points = 0) SystemsTest exists but contains no JUnit tests.  No method in it is"
					+ " annotated with @Test.  Add JUnit tests that test the main method, each one documenting"
					+ " its Preconditions, Execution Steps, and Postconditions, with assertions matching the"
					+ " Postconditions.\n\n");
			return result;
		}

		// Load a second copy of the test class in which RentACatImpl is
		// replaced by the inert implementation.
		Class<?> nullTest;
		try {
			nullTest = new NullingClassLoader(getClass().getClassLoader()).loadClass(TEST_CLASS);
		} catch (Throwable t) {
			result.setScore(0);
			result.addOutput("(points = 0) The autograder could not load SystemsTest against RentACatNull: "
					+ t + ".  Please let me know so that I can look into it.\n\n");
			return result;
		}

		for (Method m : annotated) {
			String name = m.getName();

			if (!Modifier.isPublic(m.getModifiers())) {
				points -= DEDUCTION;
				result.addOutput("(points = 0) " + name + " is annotated with @Test but is not declared"
						+ " public, so JUnit cannot run it.  Add the public keyword to it.\n\n");
				continue;
			}

			if (m.isAnnotationPresent(Ignore.class)) {
				points -= DEDUCTION;
				result.addOutput("(points = 0) " + name + " is annotated with @Ignore, so it never runs."
						+ "  Remove the @Ignore annotation.\n\n");
				continue;
			}

			Outcome onImpl = runOne(studentTest, name, TestType.SystemsTest);
			Outcome onNull = runOne(nullTest, name, TestType.SystemsTestNull);

			if (onImpl.status == Outcome.TIMED_OUT) {
				points -= DEDUCTION;
				result.addOutput("(points = 0) " + name + " did not finish within " + (TIMEOUT_MS / 1000)
						+ " seconds when run against your own RentACatImpl.  A systems test has to supply"
						+ " enough input on System.in for the main method to reach the quit option and"
						+ " return.\n\n");
				continue;
			}

			if (onImpl.status == Outcome.NOT_RUN) {
				points -= DEDUCTION;
				result.addOutput("(points = 0) " + name + " could not be run against your own RentACatImpl: "
						+ onImpl.detail + "\n\n");
				continue;
			}

			if (onImpl.status == Outcome.FAILED) {
				points -= DEDUCTION;
				result.addOutput("(points = 0) " + name + " fails on your own RentACatImpl.  A test has to"
						+ " pass on your own code before it can be credited.\n" + onImpl.detail + "\n\n");
				continue;
			}

			if (onNull.status == Outcome.PASSED) {
				points -= DEDUCTION;
				result.addOutput("(points = 0) " + name + " passes on your own RentACatImpl, but it also"
						+ " passes on RentACatNull, whose main method returns immediately without printing"
						+ " anything.  That means the test cannot tell a working application from a broken"
						+ " one.  Add assertions that check the output your Execution Steps are supposed to"
						+ " produce.\n\n");
				continue;
			}

			if (onNull.status == Outcome.TIMED_OUT) {
				points -= DEDUCTION;
				result.addOutput("(points = 0) " + name + " did not finish within " + (TIMEOUT_MS / 1000)
						+ " seconds when run against RentACatNull.  A test should not depend on the"
						+ " application producing output in order to terminate.\n\n");
				continue;
			}

			if (onNull.status == Outcome.NOT_RUN) {
				points -= DEDUCTION;
				result.addOutput("(points = 0) " + name + " could not be run against RentACatNull: "
						+ onNull.detail + "\n\n");
				continue;
			}

			result.addOutput("(points = " + (int) DEDUCTION + ") " + name + ": Good job!\n\n");
		}

		if (points < 0) {
			points = 0;
		}
		result.setScore(points);
		return result;
	}

	/**
	 * Runs a single test method on a bounded clock, with the standard streams
	 * saved and restored around it.
	 *
	 * The student's test is expected to redirect System.out and System.in
	 * itself, but if it does not, an unredirected main would read from the
	 * autograder's own stdin and print its prompts into the autograder log, so
	 * both are pointed somewhere harmless first.
	 */
	private Outcome runOne(final Class<?> testClass, final String name, TestType testType) {
		PrintStream savedOut = System.out;
		PrintStream savedErr = System.err;
		InputStream savedIn = System.in;
		TestType savedType = Config.getTestType();

		final Result[] holder = new Result[1];
		final Throwable[] thrown = new Throwable[1];

		try {
			Config.setTestType(testType);
			System.setOut(new PrintStream(new ByteArrayOutputStream()));
			System.setErr(new PrintStream(new ByteArrayOutputStream()));
			System.setIn(new ByteArrayInputStream(new byte[0]));

			Thread runner = new Thread(new Runnable() {
				public void run() {
					try {
						holder[0] = new JUnitCore().run(Request.method(testClass, name));
					} catch (Throwable t) {
						thrown[0] = t;
					}
				}
			});
			// Daemon, so that a test that will not die cannot keep the
			// autograder JVM alive after grading finishes.
			runner.setDaemon(true);
			runner.start();
			runner.join(TIMEOUT_MS);

			if (runner.isAlive()) {
				return new Outcome(Outcome.TIMED_OUT, null);
			}
		} catch (InterruptedException e) {
			return new Outcome(Outcome.NOT_RUN, e.toString());
		} finally {
			System.setOut(savedOut);
			System.setErr(savedErr);
			System.setIn(savedIn);
			Config.setTestType(savedType);
		}

		if (thrown[0] != null) {
			return new Outcome(Outcome.NOT_RUN, thrown[0].toString());
		}
		if (holder[0] == null) {
			return new Outcome(Outcome.NOT_RUN, "the test did not produce a result");
		}

		Result r = holder[0];
		if (r.getRunCount() == 0) {
			return new Outcome(Outcome.NOT_RUN, "JUnit did not run the method");
		}
		for (Failure f : r.getFailures()) {
			if (f.getTestHeader().contains(name + "(")) {
				return new Outcome(Outcome.FAILED, f.toString());
			}
		}
		if (!r.wasSuccessful()) {
			// A failure in @Before or @After is reported against the class
			// rather than the method.
			List<Failure> failures = r.getFailures();
			return new Outcome(Outcome.FAILED, failures.isEmpty() ? "" : failures.get(0).toString());
		}
		return new Outcome(Outcome.PASSED, null);
	}

	/**
	 * Loads SystemsTest so that its references to RentACatImpl resolve to the
	 * inert RentACatNull instead.
	 *
	 * A student's systems test calls RentACatImpl.main directly, and that
	 * reference is baked into the class file, so the only way to point it
	 * somewhere else is to define SystemsTest in a loader that answers
	 * differently for the name RentACatImpl. This loader is child first for
	 * exactly those two classes and delegates everything else to its parent,
	 * which keeps JUnit's annotation classes, Cat, RentACat and Config shared
	 * with the rest of the autograder.
	 *
	 * The bytes it answers with for RentACatImpl are RentACatNull's own class
	 * file with the name substituted. "RentACatNull" and "RentACatImpl" are
	 * both twelve characters, so the substitution leaves every length prefix
	 * in the constant pool valid and no bytecode library is needed. Renaming
	 * either class to a different length would break this.
	 */
	private static class NullingClassLoader extends ClassLoader {

		NullingClassLoader(ClassLoader parent) {
			super(parent);
		}

		@Override
		protected Class<?> loadClass(String name, boolean resolve) throws ClassNotFoundException {
			Class<?> already = findLoadedClass(name);
			if (already != null) {
				return already;
			}
			if (TEST_CLASS.equals(name)) {
				return define(name, read(TEST_RESOURCE));
			}
			if (IMPL_CLASS.equals(name)) {
				return define(name, rename(read(NULL_RESOURCE)));
			}
			return super.loadClass(name, resolve);
		}

		private Class<?> define(String name, byte[] bytes) {
			return defineClass(name, bytes, 0, bytes.length);
		}

		private byte[] read(String resource) throws ClassNotFoundException {
			InputStream in = SystemsTestGrading.class.getResourceAsStream(resource);
			if (in == null) {
				throw new ClassNotFoundException("resource not found: " + resource);
			}
			try {
				ByteArrayOutputStream out = new ByteArrayOutputStream();
				byte[] buffer = new byte[8192];
				int n;
				while ((n = in.read(buffer)) > 0) {
					out.write(buffer, 0, n);
				}
				return out.toByteArray();
			} catch (Exception e) {
				throw new ClassNotFoundException("could not read " + resource, e);
			} finally {
				try {
					in.close();
				} catch (Exception ignored) {
					// Nothing useful to do here.
				}
			}
		}

		/** Rewrites RentACatNull's class file to claim the name RentACatImpl. */
		private byte[] rename(byte[] bytes) {
			byte[] from = "RentACatNull".getBytes();
			byte[] to = "RentACatImpl".getBytes();
			for (int i = 0; i + from.length <= bytes.length; i++) {
				if (matches(bytes, i, from)) {
					System.arraycopy(to, 0, bytes, i, to.length);
				}
			}
			return bytes;
		}

		private boolean matches(byte[] haystack, int at, byte[] needle) {
			for (int j = 0; j < needle.length; j++) {
				if (haystack[at + j] != needle[j]) {
					return false;
				}
			}
			return true;
		}
	}
}
