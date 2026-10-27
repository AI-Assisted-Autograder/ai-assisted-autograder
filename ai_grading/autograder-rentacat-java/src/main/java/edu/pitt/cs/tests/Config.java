package edu.pitt.cs.tests;

public class Config {
	// Whether to intentionally inject bugs into CoffeeMakerQuest.
	private static TestType testType = TestType.NULL;
	
	public static void setTestType(TestType val) {
		testType = val;
	}
	public static TestType getTestType() {
		return testType;
	}
}
