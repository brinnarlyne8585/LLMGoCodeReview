
from llm_review_comment_generation.prompt_builders.base_prompt_generator import BasePromptGenerator

single_p1 = """## Instructions

You are a senior code reviewer. Review the given code snippet (in "## Code Snippet for Your Review" section) considering these possible defects:

**Functional**: Issues that may lead to incorrect code behavior (not achieving the intended behaviour or misaligned with the intent) or system failure when the code is executed. This includes:
- **Functional Defect**: Missing or incorrectly implemented functionality requiring code additions or larger modifications to the existing solution.
- **Logic Defect**: Defects made with comparison operations, control flow, and computations and other types of logical mistakes.
- **Check Defect**: Validation mistakes or mistakes made when detecting an invalid value.
- **Resource Defect**: Defects related to variable initialization, system memory management, and manipulating or releasing data or other resources.
- **Interface Defect**: Mistakes made when interacting with other parts of the software, such as an existing code library, a hardware device, a database, or an operating system.
- **Timing Defect**: Concurrency issues in multi-threaded applications involving shared resources.
- **Support Defect**: Incorrect configurations or version issues for support systems or libraries.

**Refactoring**: Issues that makes the code less compliant with standards, more error-prone, or more difficult to modify, extend, or understand. This includes:
- **Solution Approach Defect**, a wide range of defects that truly represent the Alternative Approach in its most fruitful form, including the following defects:
  - Semantic Duplication: Means syntactically different code blocks with equal intent.
  - Semantic Dead Code: Code fragments do not serve any meaningful purpose and/or have no effect on the result.
  - Change Function: Need to change a certain function call to another when the program used old or deprecated functions.
  - Use Standard Method: A standardized way of working should be used.
  - New Functionality: Need of new functionality to ensure evolvability.
- **Organization Defect**, including the following defects:
  - Move Functionality: Need to move functions, part of functions, or other functional elements to a different class, file, or module.
  - Long Sub-routine: Function, procedure or method is of excessive length and functionality.
  - Dead Code: Code that is not executed or used in the software.
  - Duplication: Code that is duplicated.
  - Complex Code: A piece of code that is difficult to comprehend.
  - Statement Issues: These require splitting, combining or otherwise reorganizing a statement inside a function.
  - Consistency: Similar code elements should operate in a similar fashion and are more or less symmetrical.
- **Alternate Output**: Comments that suggest modifying the error message, toast message, alert, or change what is returned by a function.
- **Naming Convention**: Violations of identifier naming conventions.
- **Visual Representation**: Whitespace, blank lines, code rearrangements, and indentation-related comments.
- **Documentation**: Comments to add/modify comments or documentation to aid code comprehension.

Follow these steps to conduct a focused review for the given snippet:
1. **Understand Context & Intent**: Read the given snippet and additional context (if any) to understand the original code's behavior and intent, and evaluate the modification's impact. Prioritize reviewing lines that start with "+" (added lines) or "-" (removed lines) in the code diff, then consider other lines if necessary.
2. **Multidimensional Analysis**: Analyzing the code from multiple dimensions simultaneously:
- Perspectives: Consider both Functional Defects and Refactoring Opportunities (as defined above), covering dimensions from documentation and naming to executable code logic, and ranging from statement-level details to broader architectural considerations.
- Scope: Examine various lines within the modified snippet and surrounding areas to ensure diverse coverage, rather than focusing on a single location.
- Feasibility Check: Target the focused but effective modification suggestion following the Principle of Least Change, i.e., the reported issue can be addressed with minimal, localized fixes while providing significant value. Be cautious about suggesting modifications with large-scale impact.
3. **Core Issue Identification**: From all potential issues identified in the previous step, point out **the single most significant issue**—the one that most urgently requires a fix or offers the highest value if addressed.
4. **Output Format**: Provide a focused review comment for this core issue. For this comment:
- Format it as a concise, easy-to-understand message to the developer.
- Do not include additional suggestions, background explanations, or specify defect categories."""

review_task_instruction = single_p1

class PromptGenerator(BasePromptGenerator):
    # Shared default option management.
    DEFAULT_OPTIONS = {
        "use_file_content": False,

        # Optional context configuration.
        "context_config": None,
        "context_tasks_path": None,
        "context_version": None,
    }

    def __init__(self, **options):
        """
        Allow prompt options to be overridden at construction time.
        """
        # Merge caller-provided options.
        merged_options = self.DEFAULT_OPTIONS.copy()
        merged_options.update(options or {})
        self.options = merged_options

        # Set the base instruction.
        self.base_instruction = review_task_instruction
        self._initialize_managers()

    def _render_comment_command(self) -> str:
        return f"## Your Review Comment" \
               f"\n\n" \
               f"Strictly follow the output format defined in Step 4. Provide only the comment."
