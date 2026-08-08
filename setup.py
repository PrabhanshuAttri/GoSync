from pathlib import Path

from setuptools import find_packages, setup

ROOT = Path(__file__).parent
long_description = (ROOT / "README.md").read_text(encoding="utf-8")

setup(
    name="gosync",
    version="1.4.1",
    author="Prabhanshu Attri",
    description="Self-hosted GoPro Cloud media downloader using browser HAR exports",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/PrabhanshuAttri/GoSync",
    project_urls={
        "Documentation": "https://github.com/PrabhanshuAttri/GoSync/tree/main/docs",
        "Source": "https://github.com/PrabhanshuAttri/GoSync",
    },
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Web Environment",
        "Framework :: Flask",
        "License :: OSI Approved :: GNU Affero General Public License v3",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.12",
        "Topic :: Internet :: WWW/HTTP",
        "Topic :: Multimedia",
    ],
    python_requires=">=3.12",
    install_requires=[
        "Flask>=3.1.3",
        "requests>=2.33.0",
        "tqdm>=4.66.5",
        "Werkzeug>=3.1.3",
    ],
    entry_points={
        "console_scripts": [
            "gosync=gosync.__main__:main",
        ],
    },
    include_package_data=True,
    zip_safe=False,
)
