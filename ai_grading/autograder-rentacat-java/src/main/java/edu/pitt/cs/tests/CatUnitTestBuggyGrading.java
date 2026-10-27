package edu.pitt.cs.tests;

import edu.pitt.cs.*;

import java.util.ArrayList;

import org.junit.runner.*;
import org.junit.runner.notification.*;

import org.junit.Test;
import static org.junit.Assert.*;

import org.junit.Before;
import org.junit.BeforeClass;
import org.junit.FixMethodOrder;
import org.junit.runners.MethodSorters;

import com.github.tkutche1.jgrade.*;
import com.github.tkutche1.jgrade.gradedtest.*;

@FixMethodOrder(MethodSorters.NAME_ASCENDING)
public class CatUnitTestBuggyGrading {

    private void performTest(String name) {
        Config.setTestType(TestType.CatUnitTestBuggy);
        Result _r = new JUnitCore().run(Request.method(CatUnitTest.class, name));
        Result _rsol = new JUnitCore().run(Request.method(CatUnitTestSolution.class, name));

        Failure foundF = null;
        Failure foundFSol = null;
        for (Failure f : _r.getFailures()) {
            if(f.getTestHeader().contains(name)) {
                foundF = f;
            }
        }
        for (Failure f : _rsol.getFailures()) {
            if(f.getTestHeader().contains(name)) {
                foundFSol = f;
            }
        }
        if(foundF != null && foundFSol == null) {
            fail("CatUnitTest failed but CatUnitTestSolution passed.  Failure details:\n" + foundF.toString());
        }
        if(foundF == null && foundFSol != null) {
            fail("CatUnitTest passed but CatUnitTestSolution failed.  Failure details:\n" + foundFSol.toString());
        }
    }

	@Test
	@GradedTest(name = "CatUnitTest.testGetId on CatBuggy", points = 1)
	public void testGetId() {
        performTest("testGetId");
    }

	@Test
	@GradedTest(name = "CatUnitTest.testGetName on CatBuggy", points = 1)
	public void testGetName() {
        performTest("testGetName");
    }

	@Test
	@GradedTest(name = "CatUnitTest.testGetRented on CatBuggy", points = 1)
	public void testGetRented() {
        performTest("testGetRented");
    }

	@Test
	@GradedTest(name = "CatUnitTest.testToString on CatBuggy", points = 1)
	public void testToString() {
        performTest("testToString");
    }

	@Test
	@GradedTest(name = "CatUnitTest.testRentCat on CatBuggy", points = 1)
	public void testRentCat() {
        performTest("testRentCat");
    }

	@Test
	@GradedTest(name = "CatUnitTest.testReturnCat on CatBuggy", points = 1)
	public void testReturnCat() {
        performTest("testReturnCat");
    }

	@Test
	@GradedTest(name = "CatUnitTest.testRenameCat on CatBuggy", points = 1)
	public void testRenameCat() {
        performTest("testRenameCat");
    }

}
