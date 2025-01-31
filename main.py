# main.py
# -*- coding: utf-8 -*-
import tkinter as tk
from ui import NovelGeneratorGUI

def main():
    root = tk.Tk()
    root.title("Novel Generator")
    app = NovelGeneratorGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
