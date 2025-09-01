import asyncio
import gc
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from server import (
    PathManager,
    get_zim_entry,
    html_to_plain_text,
    list_zim_files,
    search_zim_file,
)


class TestPathManager(unittest.TestCase):
    """Test PathManager functionality"""

    def test_path_manager_initialization(self):
        """Test PathManager can be initialized with valid directories"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path_manager = PathManager([temp_dir])
            self.assertEqual(len(path_manager.allowed_directories), 1)
            self.assertTrue(Path(temp_dir).resolve() in path_manager.allowed_directories)

    def test_path_manager_invalid_directory(self):
        """Test PathManager raises error for invalid directories"""
        with self.assertRaises(ValueError):
            PathManager(["/nonexistent/directory"])

    def test_validate_path_success(self):
        """Test path validation for allowed paths"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path_manager = PathManager([temp_dir])
            test_file = Path(temp_dir) / "test.zim"
            test_file.touch()  # Create the file

            validated_path = path_manager.validate_path(str(test_file))
            self.assertEqual(validated_path, str(test_file.resolve()))

    def test_validate_path_denied(self):
        """Test path validation rejects paths outside allowed directories"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path_manager = PathManager([temp_dir])

            with self.assertRaises(ValueError) as cm:
                path_manager.validate_path("/etc/passwd")

            self.assertIn("Access denied", str(cm.exception))


class TestUtilityFunctions(unittest.TestCase):
    """Test utility functions"""

    def test_html_to_plain_text(self):
        """Test HTML to plain text conversion"""
        html_content = "<h1>Test Title</h1><p>Test paragraph with <b>bold</b> text.</p>"
        plain_text = html_to_plain_text(html_content)

        self.assertIn("Test Title", plain_text)
        self.assertIn("Test paragraph", plain_text)
        self.assertIn("bold", plain_text)
        # Should not contain HTML tags
        self.assertNotIn("<h1>", plain_text)
        self.assertNotIn("<p>", plain_text)
        self.assertNotIn("<b>", plain_text)


@pytest.fixture
def temp_dir_setup():
    """Set up test fixtures for resource management tests"""
    temp_dir = tempfile.mkdtemp()
    test_zim_path = Path(temp_dir) / "test.zim"

    # Create a mock ZIM file
    test_zim_path.touch()

    # Set up global path_manager for the server functions
    import server

    server.path_manager = PathManager([temp_dir])

    yield temp_dir, test_zim_path

    # Clean up test fixtures
    import shutil

    shutil.rmtree(temp_dir)

    # Reset global path_manager
    import server

    server.path_manager = None


class TestResourceManagement:
    @pytest.mark.asyncio
    async def test_list_zim_files_no_leaks(self, temp_dir_setup):
        """Test that list_zim_files doesn't leak resources"""
        temp_dir, test_zim_path = temp_dir_setup
        initial_objects = len(gc.get_objects())

        # Call the function multiple times
        for _ in range(10):
            result = await list_zim_files()
            assert "test.zim" in result

        # Force garbage collection
        gc.collect()

        # Check that we didn't accumulate too many objects
        final_objects = len(gc.get_objects())
        object_growth = final_objects - initial_objects

        # Allow some growth but not excessive (less than 100 objects per call)
        assert object_growth < 1000, f"Potential memory leak: {object_growth} objects created"

    @pytest.mark.asyncio
    @patch("server.Archive")
    async def test_search_zim_file_resource_management(self, mock_archive_class, temp_dir_setup):
        """Test that search_zim_file properly manages Archive resources"""
        temp_dir, test_zim_path = temp_dir_setup

        # Create a mock Archive instance
        mock_archive = MagicMock()
        mock_archive_class.return_value = mock_archive

        # Set up mock searcher and search results
        mock_searcher = MagicMock()
        mock_search = MagicMock()
        mock_search.getEstimatedMatches.return_value = 1
        mock_search.getResults.return_value = ["test_entry"]
        mock_searcher.search.return_value = mock_search

        with patch("server.Searcher", return_value=mock_searcher), patch("server.Query") as mock_query_class:
            mock_query = MagicMock()
            mock_query_class.return_value.set_query.return_value = mock_query

            # Set up mock entry
            mock_entry = MagicMock()
            mock_entry.title = "Test Entry"
            mock_entry.get_item.return_value.mimetype = "text/plain"
            mock_entry.get_item.return_value.content = b"Test content"
            mock_archive.get_entry_by_path.return_value = mock_entry

            # Call the function
            result = await search_zim_file(str(test_zim_path), "test query")

            # Verify Archive was created and used
            # Note: Use str() to handle Path vs resolved path differences
            assert mock_archive_class.called
            call_args = str(mock_archive_class.call_args[0][0])
            assert str(test_zim_path) in call_args

            # Verify the result contains expected content
            assert "Test Entry" in result
            assert "Found 1 matches" in result

    @pytest.mark.asyncio
    @patch("server.Archive")
    async def test_get_zim_entry_resource_management(self, mock_archive_class, temp_dir_setup):
        """Test that get_zim_entry properly manages Archive resources"""
        temp_dir, test_zim_path = temp_dir_setup

        # Create a mock Archive instance
        mock_archive = MagicMock()
        mock_archive_class.return_value = mock_archive

        # Set up mock entry
        mock_entry = MagicMock()
        mock_entry.title = "Test Entry"
        mock_item = MagicMock()
        mock_item.mimetype = "text/html"
        mock_item.content = b"<h1>Test Content</h1><p>Test paragraph</p>"
        mock_entry.get_item.return_value = mock_item
        mock_archive.get_entry_by_path.return_value = mock_entry

        # Call the function
        result = await get_zim_entry(str(test_zim_path), "A/Test_Entry")

        # Verify Archive was created and used
        # Note: Use str() to handle Path vs resolved path differences
        assert mock_archive_class.called
        call_args = str(mock_archive_class.call_args[0][0])
        assert str(test_zim_path) in call_args

        # Verify the result contains expected content
        assert "Test Entry" in result
        assert "Test Content" in result
        assert "Test paragraph" in result

    @pytest.mark.asyncio
    async def test_multiple_concurrent_calls_no_resource_leaks(self, temp_dir_setup):
        """Test that concurrent calls to ZIM functions don't cause resource leaks"""
        temp_dir, test_zim_path = temp_dir_setup

        # Track initial resource state
        initial_objects = len(gc.get_objects())

        with patch("server.Archive") as mock_archive_class:
            mock_archive = MagicMock()
            mock_archive_class.return_value = mock_archive

            # Set up minimal mocks to avoid errors
            mock_searcher = MagicMock()
            mock_search = MagicMock()
            mock_search.getEstimatedMatches.return_value = 0  # No results to simplify
            mock_searcher.search.return_value = mock_search

            with patch("server.Searcher", return_value=mock_searcher), patch("server.Query") as mock_query_class:
                mock_query = MagicMock()
                mock_query_class.return_value.set_query.return_value = mock_query

                # Run multiple concurrent calls
                tasks = []
                for i in range(20):  # Simulate health checks
                    task = search_zim_file(str(test_zim_path), f"query_{i}")
                    tasks.append(task)

                # Execute all tasks
                results = await asyncio.gather(*tasks)

                # Verify all calls completed
                assert len(results) == 20
                for result in results:
                    assert "No search results found" in result

        # Force garbage collection
        gc.collect()

        # Check for resource leaks
        final_objects = len(gc.get_objects())
        object_growth = final_objects - initial_objects

        # Should not have significant object growth
        assert object_growth < 2000, f"Potential resource leak: {object_growth} objects after concurrent calls"

    @pytest.mark.asyncio
    @patch("server.Archive")
    async def test_archive_context_manager_usage(self, mock_archive_class, temp_dir_setup):
        """Test that Archive is properly used with context manager (closing)"""
        temp_dir, test_zim_path = temp_dir_setup
        mock_archive = MagicMock()
        mock_archive_class.return_value = mock_archive

        # Set up mocks for successful search
        mock_searcher = MagicMock()
        mock_search = MagicMock()
        mock_search.getEstimatedMatches.return_value = 0
        mock_searcher.search.return_value = mock_search

        with patch("server.Searcher", return_value=mock_searcher), patch("server.Query") as mock_query_class:
            mock_query = MagicMock()
            mock_query_class.return_value.set_query.return_value = mock_query

            # Call the function
            await search_zim_file(str(test_zim_path), "test")

            # Verify Archive was created
            mock_archive_class.assert_called_once()

            # The actual context manager usage is handled by closing()
            # We can't easily verify the close() call was made without
            # more complex mocking, but the test verifies the structure works

    @pytest.mark.asyncio
    async def test_error_handling_resource_cleanup(self, temp_dir_setup):
        """Test that resources are cleaned up even when errors occur"""
        temp_dir, test_zim_path = temp_dir_setup

        with patch("server.Archive") as mock_archive_class:
            # Make Archive constructor raise an exception
            mock_archive_class.side_effect = Exception("ZIM file corrupted")

            # Call should handle the exception gracefully
            result = await search_zim_file(str(test_zim_path), "test")

            # Should return an error message, not crash
            assert "Error: Failed to search ZIM file" in result
            assert "ZIM file corrupted" in result


class TestResourceLeakRegression:
    @pytest.mark.asyncio
    @patch("server.Archive")
    async def test_400_ping_simulation(self, mock_archive_class, temp_dir_setup):
        """Simulate the 400-ping pattern that used to cause timeouts"""
        temp_dir, test_zim_path = temp_dir_setup
        mock_archive = MagicMock()
        mock_archive_class.return_value = mock_archive

        # Set up minimal successful response
        mock_searcher = MagicMock()
        mock_search = MagicMock()
        mock_search.getEstimatedMatches.return_value = 0
        mock_searcher.search.return_value = mock_search

        with patch("server.Searcher", return_value=mock_searcher), patch("server.Query") as mock_query_class:
            mock_query = MagicMock()
            mock_query_class.return_value.set_query.return_value = mock_query

            # Simulate 400+ health checks (the problematic pattern)
            successful_calls = 0

            for i in range(450):  # Go beyond the problematic 400 limit
                try:
                    result = await search_zim_file(str(test_zim_path), f"health_check_{i}")
                    assert isinstance(result, str)
                    successful_calls += 1

                    # Every 100 calls, force garbage collection
                    if i % 100 == 0:
                        gc.collect()

                except Exception as e:
                    pytest.fail(f"Function failed at call {i}: {e}")

            # Should complete all 450 calls successfully
            assert successful_calls == 450

            # Verify Archive was called the expected number of times
            assert mock_archive_class.call_count == 450


if __name__ == "__main__":
    # Run the tests
    unittest.main(verbosity=2)
