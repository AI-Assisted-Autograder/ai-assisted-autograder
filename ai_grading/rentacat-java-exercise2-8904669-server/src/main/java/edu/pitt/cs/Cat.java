package edu.pitt.cs;

import edu.pitt.cs.tests.*;

import org.mockito.*;
import static org.junit.Assert.*;
import static org.hamcrest.CoreMatchers.*;

public interface Cat {
	public static Cat createInstance(InstanceType type, int id, String name) {
		switch (Config.getTestType()) {
			case CatUnitTestSolution:
				assertThat(
						"Mock Cat used while unit testing Cat.  This would test a fake version of Cat.  Is this what you want?",
						type, is(not(InstanceType.MOCK)));
				return new CatSolution(id, name);
			case CatUnitTestBuggy:
				assertThat(
						"Mock Cat used while unit testing Cat.  This would test a fake version of Cat.  Is this what you want?",
						type, is(not(InstanceType.MOCK)));
				return new CatBuggy(id, name);
			case RentACatIntegrationTestSolution:
				assertThat(
						"Mock Cat used while integration testing.  This would not test the real cat.  Is this what you want?",
						type, is(not(InstanceType.MOCK)));
				return new CatSolution(id, name);
			case RentACatIntegrationTestBuggy:
				assertThat(
						"Mock Cat used while integration testing.  This would not test the real cat.  Is this what you want?",
						type, is(not(InstanceType.MOCK)));
				return new CatBuggy(id, name);
			case RentACatTest:
				assertEquals("Non-impl instance used for Cat implementation testing", InstanceType.IMPL, type);
				return new CatImpl(id, name);
			case SystemsTest:
			case SystemsTestNull:
				assertEquals("Non-impl instance used for systems testing", InstanceType.IMPL, type);
				return new CatImpl(id, name);
			case RentACatUnitTestSolution:
			case RentACatUnitTestBuggy:
				assertEquals(
						"Real cats used while unit testing RentACat.  This would cause defects in Cat to fail RentACat unit tests.  Is this what you want?",
						InstanceType.MOCK, type);

				Cat c = Mockito.mock(Cat.class);
				Mockito.when(c.getId()).thenReturn(id);
				Mockito.when(c.getName()).thenReturn(name);
				Mockito.when(c.toString()).thenReturn("ID " + id + ". " + name);
				return c;
			default:
				fail("Unknown test type " + Config.getTestType());
				return null;
		}
	}

	public void rentCat();

	public void returnCat();

	public void renameCat(String name);

	public String getName();

	public int getId();

	public boolean getRented();

	public String toString();
}
