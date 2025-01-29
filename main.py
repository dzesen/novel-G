import tkinter as tk
from ui import NovelGeneratorGUI

def main():
    root = tk.Tk()
    root.title("Novel Generator - Innovative Flow")
    app = NovelGeneratorGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
