package edu.pitt.cs.tests;

import edu.pitt.cs.*;

import java.util.ArrayList;

import org.junit.runner.*;
import org.junit.runner.notification.*;

import org.junit.Test;
import static org.junit.Assert.*;

import org.junit.BeforeClass;
import org.junit.FixMethodOrder;
import org.junit.runners.MethodSorters;

import com.github.tkutche1.jgrade.*;
import com.github.tkutche1.jgrade.gradedtest.*;

@FixMethodOrder(MethodSorters.NAME_ASCENDING)
public class CatUnitTestSolutionGrading {

    private void performTest(String name) {
        Config.setTestType(TestType.CatUnitTestSolution);
        Result _r = new JUnitCore().run(Request.method(CatUnitTest.class, name));

        for (Failure f : _r.getFailures()) {
            if(f.getTestHeader().contains(name)) {
                fail(f.toString());
            }
        }
    }

	@Test
	@GradedTest(name = "CatUnitTest.testGetId on CatSolution", points = 1)
	public void testGetId() {
        performTest("testGetId");
    }

	@Test
	@GradedTest(name = "CatUnitTest.testGetName on CatSolution", points = 1)
	public void testGetName() {
        performTest("testGetName");
    }

	@Test
	@GradedTest(name = "CatUnitTest.testGetRented on CatSolution", points = 1)
	public void testGetRented() {
        performTest("testGetRented");
    }

	@Test
	@GradedTest(name = "CatUnitTest.testToString on CatSolution", points = 1)
	public void testToString() {
        performTest("testToString");
    }

	@Test
	@GradedTest(name = "CatUnitTest.testRentCat on CatSolution", points = 1)
	public void testRentCat() {
        performTest("testRentCat");
    }

	@Test
	@GradedTest(name = "CatUnitTest.testReturnCat on CatSolution", points = 1)
	public void testReturnCat() {
        performTest("testReturnCat");
    }

	@Test
	@GradedTest(name = "CatUnitTest.testRenameCat on CatSolution", points = 1)
	public void testRenameCat() {
        performTest("testRenameCat");
    }

}
