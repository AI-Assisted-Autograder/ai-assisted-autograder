import com.github.tkutche1.jgrade.AfterGrading;
import com.github.tkutche1.jgrade.BeforeGrading;
import com.github.tkutche1.jgrade.Grade;
import com.github.tkutche1.jgrade.Grader;
import com.github.tkutche1.jgrade.gradescope.GradescopeJsonObserver;

/**
 * JGrade entry point (see run.sh).  All of the actual grading logic lives in
 * RentACatTestGrading.
 *
 * No SecurityManager is installed: no student Java code runs in this JVM, and
 * System.setSecurityManager() throws on Java 18+.
 */
public class MainGrader {

    private GradescopeJsonObserver observer;

    @BeforeGrading
    public void setUp(Grader grader) {
        observer = new GradescopeJsonObserver();
        grader.attachOutputObserver(observer);
        grader.setMaxScore(RentACatTestGrading.MAX_SCORE);
        grader.startTimer();
    }

    @AfterGrading
    public void tearDown(Grader grader) {
        grader.stopTimer();
        grader.notifyOutputObservers();
        observer.setPrettyPrint(4);
        System.out.println(observer.toString());
    }

    @Grade
    public void gradeRentACat(Grader grader) {
        new RentACatTestGrading().grade(grader);
    }
}
