# Terminal demo for the README. Regenerate with:  make demo
# Runs the real CLI on the committed examples; nothing is staged.

Output docs/demo.gif

Set Shell "bash"
Set FontSize 14
Set Width 1180
Set Height 900
Set Padding 18
Set Theme "GitHub Dark"
Set TypingSpeed 30ms

Hide
Type `export PYTHONPATH=/vhs/src PS1='$ ' && paramguard() { python3 -m paramguard "$@" --color always; } && cd /vhs/examples/survey-quad && clear`
Enter
Show

Type "diff vehicle.params cleanup.params | grep -c '^<'   # a cleanup PR: 17 lines deleted"
Enter
Sleep 1500ms
Type "paramguard check vehicle.params cleanup.params"
Enter
Sleep 7s

Type "clear"
Enter
Sleep 300ms
Type "paramguard check vehicle.params tuning.params"
Enter
Sleep 10s
