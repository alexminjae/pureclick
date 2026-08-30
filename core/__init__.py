"""Pure logic, shared by the panel and the in-page autopilot's tests.

Nothing here touches tkinter, pywebview or the filesystem. That is the whole
point of the split: these are the parts that can be tested without launching an
app or opening a booking page, and every one of them is.
"""
