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
import org.mockito.*;

import com.github.tkutche1.jgrade.*;
import com.github.tkutche1.jgrade.gradedtest.*;

@FixMethodOrder(MethodSorters.NAME_ASCENDING)
public class RentACatUnitTestSolutionGrading {

    private void performTest(String name) {
        Config.setTestType(TestType.RentACatUnitTestSolution);
        Result _r = new JUnitCore().run(Request.method(RentACatUnitTest.class, name));

        Cat c = CatSubmission.createInstance(InstanceType.MOCK, 1, "Jennyanydots");
        assertNotNull("Null returned by factory method Cat.createInstance(InstanceType.MOCK, 1, \"Jennyanydots\")", c);
        assertTrue("Non-mock object returned by factory method Cat.createInstance(InstanceType.MOCK, 1, \"Jennyanydots\")", Mockito.mockingDetails(c).isMock());
        assertEquals("Cat returned by factory method Cat.createInstance(InstanceType.MOCK, 1, \"Jennyanydots\") does not have ID 1", 1, c.getId());
        assertEquals("Cat returned by factory method Cat.createInstance(InstanceType.MOCK, 1, \"Jennyanydots\") does not have name Jennyanydots", "Jennyanydots", c.getName());
        assertEquals("Cat returned by factory method Cat.createInstance(InstanceType.MOCK, 1, \"Jennyanydots\") does not return \"ID 1. Jennyanydots\" on toString()", "ID 1. Jennyanydots", c.toString());
        assertFalse("Cat returned by factory method Cat.createInstance(InstanceType.MOCK, 1, \"Jennyanydots\") starts out as rented", c.getRented());

        for (Failure f : _r.getFailures()) {
            if(f.getTestHeader().contains(name)) {
                fail(f.toString());
            }
        }
    }

	@Test
	@GradedTest(name = "RentACatUnitTest.testGetCatNullNumCats0 on RentACatSolution", points = 1)
	public void testGetCatNullNumCats0() {
        performTest("testGetCatNullNumCats0");
    }

	@Test
	@GradedTest(name = "RentACatUnitTest.testGetCatNumCats3 on RentACatSolution", points = 1)
	public void testGetCatNumCats3() {
        performTest("testGetCatNumCats3");
    }

	@Test
	@GradedTest(name = "RentACatUnitTest.testListCatsNumCats0 on RentACatSolution", points = 1)
	public void testListCatsNumCats0() {
        performTest("testListCatsNumCats0");
    }

	@Test
	@GradedTest(name = "RentACatUnitTest.testListCatsNumCats3 on RentACatSolution", points = 1)
	public void testListCatsNumCats3() {
        performTest("testListCatsNumCats3");
    }

    @Test
	@GradedTest(name = "RentACatUnitTest.testRenameFailureNumCats0 on RentACatSolution", points = 1)
	public void testRenameFailureNumCats0() {
        performTest("testRenameFailureNumCats0");
    }

    @Test
	@GradedTest(name = "RentACatUnitTest.testRenameNumCat3 on RentACatSolution", points = 1)
	public void testRenameNumCat3() {
        performTest("testRenameNumCat3");
    }

	@Test
	@GradedTest(name = "RentACatUnitTest.testRentCatNumCats3 on RentACatSolution", points = 1)
	public void testRentCatNumCats3() {
        performTest("testRentCatNumCats3");
    }

	@Test
	@GradedTest(name = "RentACatUnitTest.testRentCatFailureNumCats3 on RentACatSolution", points = 1)
	public void testRentCatFailureNumCats3() {
        performTest("testRentCatFailureNumCats3");
    }

	@Test
	@GradedTest(name = "RentACatUnitTest.testReturnCatNumCats3 on RentACatSolution", points = 1)
	public void testReturnCatNumCats3() {
        performTest("testReturnCatNumCats3");
    }

	@Test
	@GradedTest(name = "RentACatUnitTest.testReturnFailureCatNumCats3 on RentACatSolution", points = 1)
	public void testReturnFailureCatNumCats3() {
        performTest("testReturnFailureCatNumCats3");
    }

}
