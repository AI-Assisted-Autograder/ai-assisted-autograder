package edu.pitt.cs.tests;

import edu.pitt.cs.*;

import java.security.Permission;
import org.junit.FixMethodOrder;
import org.junit.runners.MethodSorters;

import com.github.tkutche1.jgrade.*;
import com.github.tkutche1.jgrade.gradedtest.*;
import com.github.tkutche1.jgrade.gradescope.GradescopeJsonObserver;

@FixMethodOrder(MethodSorters.NAME_ASCENDING)
public class MainGrader {

    static SecurityManager old = System.getSecurityManager(); // Save current Security Manager
    
    private GradescopeJsonObserver observer;
    
    @BeforeGrading
    public void setUp(Grader grader) {
        System.setSecurityManager(new SecurityManager() {
            @Override
            public void checkExit(int status) {
                super.checkExit(status);
                throw new SecurityException("You are not allowed to use System.exit()!");
            }

            @Override
            public void checkPermission(Permission perm) {
                // Allow the security manager to be replaced.
            }
        });
        
        observer = new GradescopeJsonObserver();
        grader.attachOutputObserver(observer);
        grader.setMaxScore(90.0);
        grader.startTimer();
    }
    
    @AfterGrading
    public void tearDown(Grader grader) {
        grader.stopTimer();
        grader.notifyOutputObservers();
        observer.setPrettyPrint(4);
        System.out.println(observer.toString());
        
        System.setSecurityManager(old);
    }
    
    @Grade
    public void run1CatUnitTestSolutionGrading(Grader grader) {
        grader.runJUnitGradedTests(CatUnitTestSolutionGrading.class);
    }

    @Grade
    public void run2CatUnitTestBuggyGrading(Grader grader) {
        grader.runJUnitGradedTests(CatUnitTestBuggyGrading.class);
    }

    @Grade
    public void run3RentACatUnitTestSolutionGrading(Grader grader) {
        grader.runJUnitGradedTests(RentACatUnitTestSolutionGrading.class);
    }

    @Grade
    public void run4RentACatUnitTestBuggyGrading(Grader grader) {
        grader.runJUnitGradedTests(RentACatUnitTestBuggyGrading.class);
    }

    @Grade
    public void run5RentACatIntegrationTestSolutionGrading(Grader grader) {
        grader.runJUnitGradedTests(RentACatIntegrationTestSolutionGrading.class);
    }

    @Grade
    public void run6RentACatIntegrationTestBuggyGrading(Grader grader) {
        grader.runJUnitGradedTests(RentACatIntegrationTestBuggyGrading.class);
    }

    @Grade
    public void run7RentACatGrading(Grader grader) {
        grader.runJUnitGradedTests(RentACatGrading.class);
    }

    @Grade
    public void run8SystemsTestGrading(Grader grader) {
        SystemsTestGrading testGrading = new SystemsTestGrading();
        GradedTestResult result = testGrading.test();
        grader.addGradedTestResult(result);
    }

}
