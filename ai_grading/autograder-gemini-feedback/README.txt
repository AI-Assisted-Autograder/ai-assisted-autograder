CS101 Test Assignment Autograder with Gemini Feedback
===================================================

Scope
-----
This autograder targets the CS101 test assignment using SortedCollection.java.
It adds the existing AIGrading module to the original assignment autograder.
Reusability for future assignments remains to be determined and requires
further validation. Configurable source paths and rubrics do not establish
compatibility with other assignments.

Original package: ../autograder/
Upload package: ../gemini autograder.zip

Original grading workflow
-------------------------
- Update the functional tests from GitHub.
- Copy SortedCollection.java and remove its package declaration.
- Compile the submission and report compiler errors using the original templates.
- Run the original functional tests and generate results.json.

Changes to existing files
-------------------------
setup.sh
  Adds python3-venv and installs the AI module dependencies from requirements.txt.
run_autograder
  Calls python -m ai_grading.gradescope once after the original tests finish.

The added sections are marked BEGIN GEMINI FEEDBACK / END GEMINI FEEDBACK.
Removing those sections restores the original scripts, apart from LF line endings.

Added runtime files
-------------------
ai_grading/
  Includes the existing AIGradingComponent, FileResolver, PromptBuilder, models,
  GeminiProvider, provider interface, response parser, and .gemini.json configuration.
  gradescope.py is the added integration entry point. It reads the submitted code,
  rubric, and original test results, calls AIGradingComponent, and appends feedback
  as zero-point entries in results.json. Functional-test scores remain unchanged.
ai_feedback.json
  Configures the source-file paths and feedback rubrics for this test assignment.
requirements.txt
  Lists the Python dependencies: pydantic, httpx, and google-genai.

Unchanged files: compile_error.prefix, compile_error.postfix, ssh_config, deploy_key.
README.txt and .gitignore are documentation and repository support files;
they are excluded from the upload ZIP.

The integration uses the existing AI module's interfaces, validation, and retry
behavior. It adds no separate assignment-grading helper or defensive grading
workflow. If an AI call fails, it appends an unavailable-feedback message while
preserving the functional-test scores.

Usage
-----
- Upload ../autograder-gemini-feedback.zip with the Gradescope Ubuntu 22.04 image.
- The Gemini model and API key use ai_grading/ILLMProvider/.gemini.json.
- In ai_feedback.json, targets[].source selects a source file and rubric defines
  the feedback requirements.
- Compilation failures follow the original exit branch. Successful compilation
  proceeds to the original functional tests and then AI feedback.
- The ZIP contains the GitHub deploy key and Gemini configuration and is intended
  for instructor upload.

Verification materials are in ../verification/autograder-gemini-feedback/
and are excluded from the deployment package.
