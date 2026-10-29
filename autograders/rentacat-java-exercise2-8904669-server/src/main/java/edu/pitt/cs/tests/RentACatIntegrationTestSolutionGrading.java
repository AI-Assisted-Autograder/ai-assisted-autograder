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
public class RentACatIntegrationTestSolutionGrading {

    private void performTest(String name) {
        Config.setTestType(TestType.RentACatIntegrationTestSolution);
        Result _r = new JUnitCore().run(Request.method(RentACatIntegrationTest.class, name));

        for (Failure f : _r.getFailures()) {
            if(f.getTestHeader().contains(name)) {
                fail(f.toString());
            }
        }
    }

	@Test
	@GradedTest(name = "RentACatIntegrationTest.testGetCatNullNumCats0 on RentACatSolution", points = 1)
	public void testGetCatNullNumCats0() {
        performTest("testGetCatNullNumCats0");
    }

	@Test
	@GradedTest(name = "RentACatIntegrationTest.testGetCatNumCats3 on RentACatSolution", points = 1)
	public void testGetCatNumCats3() {
        performTest("testGetCatNumCats3");
    }

	@Test
	@GradedTest(name = "RentACatIntegrationTest.testListCatsNumCats0 on RentACatSolution", points = 1)
	public void testListCatsNumCats0() {
        performTest("testListCatsNumCats0");
    }

	@Test
	@GradedTest(name = "RentACatIntegrationTest.testListCatsNumCats3 on RentACatSolution", points = 1)
	public void testListCatsNumCats3() {
        performTest("testListCatsNumCats3");
    }

    @Test
	@GradedTest(name = "RentACatIntegrationTest.testRenameFailureNumCats0 on RentACatSolution", points = 1)
	public void testRenameFailureNumCats0() {
        performTest("testRenameFailureNumCats0");
    }

    @Test
	@GradedTest(name = "RentACatIntegrationTest.testRenameNumCat3 on RentACatSolution", points = 1)
	public void testRenameNumCat3() {
        performTest("testRenameNumCat3");
    }

	@Test
	@GradedTest(name = "RentACatIntegrationTest.testRentCatNumCats3 on RentACatSolution", points = 1)
	public void testRentCatNumCats3() {
        performTest("testRentCatNumCats3");
    }

	@Test
	@GradedTest(name = "RentACatIntegrationTest.testRentCatFailureNumCats3 on RentACatSolution", points = 1)
	public void testRentCatFailureNumCats3() {
        performTest("testRentCatFailureNumCats3");
    }

	@Test
	@GradedTest(name = "RentACatIntegrationTest.testReturnCatNumCats3 on RentACatSolution", points = 1)
	public void testReturnCatNumCats3() {
        performTest("testReturnCatNumCats3");
    }

	@Test
	@GradedTest(name = "RentACatIntegrationTest.testReturnFailureCatNumCats3 on RentACatSolution", points = 1)
	public void testReturnFailureCatNumCats3() {
        performTest("testReturnFailureCatNumCats3");
    }

}
