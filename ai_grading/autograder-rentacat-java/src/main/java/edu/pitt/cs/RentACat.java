package edu.pitt.cs;

import edu.pitt.cs.tests.*;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertThat;
import static org.junit.Assert.fail;
import static org.hamcrest.CoreMatchers.*;

import org.mockito.Mockito;

public interface RentACat {
	public static RentACat createInstance(InstanceType type) {
		switch (Config.getTestType()) {
			case CatUnitTestSolution:
			case CatUnitTestBuggy:
				fail("Do you really need to create RentACat in order to test Cat?");
			case RentACatIntegrationTestSolution:
			case RentACatUnitTestSolution:
				assertThat("Mock RentACat used while testing RentACat.  This would test a fake version of RentACat.  Is this what you want?", type, is(not(InstanceType.MOCK)));
				return new RentACatSolution();
			case RentACatIntegrationTestBuggy:
			case RentACatUnitTestBuggy:
				assertThat("Mock RentACat used while testing RentACat.  This would test a fake version of RentACat.  Is this what you want?", type, is(not(InstanceType.MOCK)));
				return new RentACatBuggy();
			case RentACatTest:
				assertEquals("Non-impl instance used for RentACat implementation testing", InstanceType.IMPL, type);
				return new RentACatImpl();
			case SystemsTest:
				assertEquals("Non-impl instance used for systems testing", InstanceType.IMPL, type);
				return new RentACatImpl();
			case SystemsTestNull:
				assertEquals("Non-impl instance used for systems testing", InstanceType.IMPL, type);
				return new RentACatNull();
			default:
				fail("Unknown test type " + Config.getTestType());
				return null;
		}
	}
	
	public boolean returnCat(int id);
	public boolean rentCat(int id);
	public boolean renameCat(int id, String name);
	public String listCats();
	public void addCat(Cat c);
}
