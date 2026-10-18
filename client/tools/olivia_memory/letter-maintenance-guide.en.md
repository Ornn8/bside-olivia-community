# Compare and tidy letters

Since 2.0.12, Olivia's settings page has a section called “Compare and tidy letters”. You will find it under
“Letters & memory → Import and export letters”. It is folded away by default; click its title to open it.

It deals with this kind of trouble: old letters you brought over from Lingli (灵离) show strange times; the same letter
appears twice in the mailbox; or a few letters sit there showing “failed” and cannot be removed.

While it works, it does not touch the text of any letter, does not touch Linli's long-term memory, and does not call
the large model. You do not even have to quit Olivia. And whatever you tidy can be undone at any time: a letter you
fold away is not deleted, it is still in place and can be put back whenever you like.

## What it looks like

Once opened, a line at the top tells you it can also compare against a backup:

> Check first, then choose the letters to tidy. You can compare against a .soul or JSON backup, fix times and order,
> and fold away duplicate and failed letters. Tidying only changes what the mailbox shows; it doesn't touch the original
> text or long-term memory, and doesn't call a model. You can restore here.

Below it are three buttons.

“Check current mailbox” needs no file. Click it and it reads your mailbox as it is now and lists the letters with problems.

“Choose a backup to compare / fix times” asks you to pick a file: Lingli's “.soul”, or the
“Olivia-letters-xxxx-xx-xx.json” you exported from Olivia earlier. After you pick it, it matches the backup against
the letters in your mailbox one by one.

“Apply selected” is the last step: it puts the actions you picked into effect. It asks once more before it does.

Under the buttons there are previous / next page controls, with thirty entries to a page.

## The times are wrong: how to fix them

Click “Choose a backup to compare / fix times” and pick that “.soul”. After a moment the status line tells you how much it found:

> Compared file: xxx.soul. N items in all: exact duplicates x, near duplicates y, times that can be fixed z, failed letters q, restorable l.

Scroll down and find the entries marked “Time and order can be fixed”. Each one has two columns side by side: the left one is
the letter in your mailbox, the right one is the letter in the backup, each with its time shown above it (in Beijing time).
What you need to check is whether the two are really the same letter, and whether the time on the backup side is right.

Under each entry there is an “Action” drop-down, set to “Leave as is” by default. To fix it, choose “Fix the time and order from the backup”;
it will follow the date and position of the letter in the backup. Once a page is done, click “Apply selected”, confirm, and then go to the next page.

One thing is easy to trip over: turning the page clears the selection on the current page. The page says “Turning the page clears
this page's selection, so apply first.” So work one page at a time: choose, apply, then turn the page. Don't flip all the way to the end and then think about going back.

Also, only one backup file is compared at a time. If you change files halfway through, check again from the start.

If an entry says “Text differs · compare only, no automatic overwrite or time fix”, the letter in the backup and the one
in the mailbox have different text. These are only shown to you and never changed for you. In that case you usually want to fold away a duplicate
(see below), not fix a time.

## Duplicate letters in the mailbox

No file is needed for this. Just click “Check current mailbox”.

It sorts the problems into groups, and each group is handled a little differently:

- “Exact duplicate”: the letter and reply are identical, word for word. Choose “Keep the right one, fold away the left one”: keep one and fold away the rest.
- “Near duplicate · compare both sides' text”: very similar but not identical. You have to look at both sides yourself and decide which version to keep.
- “Time and order can be fixed”: the backup has the same letter with a date, so you can simply follow it (you can only see these once you have chosen a file).
- “Failed or cancelled letter”: letters that have been stuck for good; you can fold them away from the mailbox directly.
- “Letter imported by an older tool”: clearly brought in by an outside tool; these can be folded away too.
- “Tidied · restorable”: things you tidied before. Choose “Restore the display and times from before tidying” to put them back.
- “Same content” and “Only in the mailbox”: nothing is wrong. They are listed only so you know; leave them alone.
- “Only in the backup”: the backup has it and the mailbox does not. To get it in, go back to “Choose a file to import” above and import it.
- “The backup has several dates · not fixing for now”: the same text is paired with several dates in the backup, so it cannot tell which is right and leaves it unchanged.
  This is deliberately cautious, so that nothing gets changed wrongly. You can ignore it.

“Fold away” is not “delete”. The text of a folded letter is still there; it is just no longer shown in the mailbox. Next time you come in it
appears as “Tidied · restorable”, and one choice brings it back. So you can try this step without worry; nothing gets lost.

## What exactly does it touch?

You can rest easy on this:

- The text of the letters is not changed by a single character.
- Linli's long-term memory is not touched.
- No model is called.
- You do not need to quit Olivia.
- The memory database is not written to either. The result of tidying is recorded only in the mailbox's own state.

One more detail: when Olivia exports a letter backup, and when it decides whether an imported letter is a duplicate, it looks at
the letters as they were before tidying. An exported backup keeps the original recorded times and does not carry this computer's display
tidying; on another computer you need to tidy the display again.

## This is not the same as “importing”

Easy to confuse: “do the times look right” and “does Linli remember” are two different things.

“Compare and tidy letters” only affects how the mailbox looks. However neatly you fix it, Linli will not remember one more word because of it.
For her to remember, use “Choose a file to import” to write the letters into the memory archive, then leave Olivia running; in the background
the letters are slowly fed into the search index (roughly twenty letters every five seconds), and after every five letters the model is asked
to assess the relationship. This takes a while, and the settings page shows its progress.

So the order is: import first, leave it running until it finishes, then come back and tidy the display.

## Backup times and repeated imports

The same moment written as Beijing time, as UTC or as a numeric timestamp is treated as one time when checking for duplicates; older times without a
time zone are read as Beijing time. JSON backups import normally. Letters that were imported twice are not deleted automatically; you can check
them in “Compare and tidy letters” and fold them away.

## When a message is hard to understand

A few messages may appear on the page. This is what they mean:

- “The backup can't be read. Please choose a complete .soul or JSON file (the text list is limited to 16 MB).”: the wrong file, or it is too big.
- “The mailbox changed. Please check again before choosing.”: the mailbox was changed somewhere else between your check and your apply. Just check again.
- “Memories are being imported or tidied. Please check again shortly.”: something else is writing right now. Wait a moment and come back.
- “Couldn't save the tidy-up result. Please check your disk and try again.”: most likely the disk is full.
- “The chosen actions conflict or the backup format is invalid…”: two clashing actions were chosen for the same letter. Check again and choose once more.
- “There are many letters, so only part of the near-duplicate comparison was done; the exact-duplicate check covered everything.”: the
  near-duplicate comparison has a limit, but exact duplicates are always compared in full, so it does not stop you from finding duplicate letters.

If “Check current mailbox” says there is nothing to tidy, the mailbox really is clean and you can leave it.

## When to use another tool

Olivia also comes with a small tool (in the “tools\olivia_memory\” folder of the version folder under “install\versions\local_backend\”;
double-click “运行.bat” to open it). You no longer need it for everyday mailbox display fixes, but a few things only it can do (it comes with detailed illustrated instructions):

- A health check: see how many letters are in the memory archive and how many entries are in the search index.
- Rolling Olivia back to an earlier version (in case a patch leaves the game unable to open).
- Correcting the date of a letter in the memory archive as well. “Compare and tidy letters” does not write to the database and
  only changes how the mailbox looks; if you want the dates Linli sees when she recalls old letters to be right too, you need that tool.
