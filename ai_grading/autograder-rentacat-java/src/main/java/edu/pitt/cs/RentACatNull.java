package edu.pitt.cs;

/**
 * A deliberately inert RentACat, used by SystemsTestGrading to check that the
 * student's systems tests are meaningful. Its main method returns immediately
 * without printing anything, and none of its other methods do anything
 * either, so any systems test that makes a real assertion about the
 * application's behavior must fail against this class.
 *
 * SystemsTestGrading loads this class in place of RentACatImpl, so the class
 * name here has to stay exactly as long as "RentACatImpl". See the comment on
 * NullingClassLoader in SystemsTestGrading before renaming it.
 */
public class RentACatNull implements RentACat {

	public boolean returnCat(int id) {
		return false;
	}

	public boolean rentCat(int id) {
		return false;
	}

	public boolean renameCat(int id, String name) {
		return false;
	}

	public String listCats() {
		return "";
	}

	public void addCat(Cat c) {
	}

	/**
	 * Returns immediately, printing nothing.
	 *
	 * @param args - IGNORED, kept for compatibility
	 */
	public static void main(String[] args) {
		return;
	}
}
